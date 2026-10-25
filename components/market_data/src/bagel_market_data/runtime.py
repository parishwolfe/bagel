"""One stream owner with independent buffering, persistence, and REST repair."""

import asyncio
import logging
import random
import signal
import time
from contextlib import ExitStack, asynccontextmanager
from datetime import UTC, datetime, timedelta

import psycopg

from .capacity import load_watchlist
from .config import Settings
from .domain import Observation, TimeRange
from .historical import ingest_history
from .observability import Health, Metrics, serve_health
from .providers.alpaca import AlpacaREST, AlpacaStream, TransientProviderError
from .schedule import closure_delay, collection_window
from .storage import PostgresStore, StreamAlreadyOwned
from .watchlist import CapacityExceeded, WatchlistChanged, configuration_signature

log = logging.getLogger(__name__)


async def collect(
    settings: Settings, provider, stream, queue: asyncio.Queue, health: Health, metrics: Metrics
) -> None:
    failures = closed_attempts = 0
    previous_reason = None
    while True:
        try:
            now = datetime.now(UTC)
            status = await asyncio.to_thread(collection_window, settings, provider, now)
            health.session_active = status.active
            if not status.allowed:
                delay = closure_delay(closed_attempts, now, status.next_start)
                closed_attempts += 1
                if status.reason != previous_reason:
                    log.info(
                        "collection_paused",
                        extra={
                            "reason": status.reason,
                            "seconds": delay,
                            "next_start": status.next_start.isoformat()
                            if status.next_start
                            else None,
                        },
                    )
                previous_reason = status.reason
                await asyncio.sleep(delay)
                continue
            closed_attempts = 0
            if previous_reason != status.reason:
                log.info("collection_open", extra={"feed": settings.feed})
            previous_reason = status.reason

            async def receive():
                nonlocal failures
                async for bar, origin in stream.events(settings.symbols):
                    received = datetime.now(UTC)
                    day = status.day  # noqa: B023 - monitor refreshes this shared status.
                    session = (
                        day.classify(bar.timestamp, settings.window_open, settings.window_close)
                        if day
                        else None
                    )
                    if session:
                        async with asyncio.timeout(10):
                            await queue.put(Observation(bar, session, origin, received, received))
                        metrics.received.inc()
                        metrics.queue_depth.set(queue.qsize())
                    failures = 0

            async def monitor():
                nonlocal status
                while True:
                    await asyncio.sleep(60)
                    status = await asyncio.to_thread(
                        collection_window, settings, provider, datetime.now(UTC)
                    )
                    health.session_active = status.active
                    if not status.allowed:
                        return  # Recheck calendar/clock even when no bars arrive.

            reader, watcher = asyncio.create_task(receive()), asyncio.create_task(monitor())
            try:
                done, _ = await asyncio.wait([reader, watcher], return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                reader.cancel()
                watcher.cancel()
                await asyncio.gather(reader, watcher, return_exceptions=True)
        except (TransientProviderError, TimeoutError) as exc:
            failures += 1
            delay = min(60, 2 ** min(failures, 6)) + random.uniform(0, 1)
            metrics.reconnects.inc()
            log.warning(
                "stream_reconnect", extra={"seconds": delay, "error_type": type(exc).__name__}
            )
            await asyncio.sleep(delay)


async def write_batches(
    settings: Settings, store, series, queue: asyncio.Queue, health: Health, metrics: Metrics
) -> None:
    stopping = False
    while not stopping:
        batch = []
        deadline = time.monotonic() + settings.flush_seconds
        while len(batch) < settings.batch_size:
            try:
                item = await asyncio.wait_for(queue.get(), max(0.001, deadline - time.monotonic()))
            except TimeoutError:
                break
            if item is None:
                queue.task_done()
                stopping = True
                break
            batch.append(item)
        if not batch:
            continue
        failures = 0
        while True:
            try:
                await asyncio.to_thread(store.write_live, series, batch)
                health.database_ok = True
                metrics.written.inc(len(batch))
                metrics.lag.set(
                    max(
                        0,
                        (
                            datetime.now(UTC) - max(item.bar.timestamp for item in batch)
                        ).total_seconds(),
                    )
                )
                for _ in batch:
                    queue.task_done()
                metrics.queue_depth.set(queue.qsize())
                break
            except (
                psycopg.OperationalError,
                psycopg.errors.SerializationFailure,
                psycopg.errors.DeadlockDetected,
            ) as exc:
                health.database_ok = False
                failures += 1
                metrics.write_failures.inc()
                log.warning("batch_write_retry", extra={"error_type": type(exc).__name__})
                await asyncio.sleep(min(30, 2 ** min(failures, 5)))


async def repair(
    settings: Settings,
    provider,
    store,
    series,
    stream_key: str,
    health: Health,
    metrics: Metrics,
    wake: asyncio.Event | None = None,
) -> None:
    default = (settings.eligible_end() - timedelta(days=settings.reconcile_days)).replace(
        hour=0, minute=0
    )
    cursor = await asyncio.to_thread(store.repair_cursor, stream_key, default)
    # Container hosts can have less than one reconciliation interval of uptime.
    # Always run the first correction sweep, independent of the monotonic epoch.
    last_full = float("-inf")
    while True:
        try:
            end = settings.eligible_end()
            metrics.repair_backlog.set(max(0, (end - cursor).total_seconds()))
            if cursor < end:

                def checkpoint(chunk, count):
                    store.advance_cursor(stream_key, chunk.end)
                    metrics.written.inc(count)

                await asyncio.to_thread(
                    ingest_history,
                    provider,
                    store,
                    settings,
                    TimeRange(cursor, end),
                    series=series,
                    on_complete=checkpoint,
                )
                cursor = end
            if time.monotonic() - last_full >= settings.reconcile_interval_seconds:
                start = (end - timedelta(days=settings.reconcile_days)).replace(hour=0, minute=0)
                await asyncio.to_thread(
                    ingest_history,
                    provider,
                    store,
                    settings,
                    TimeRange(start, end),
                    series=series,
                    force=True,
                )
                last_full = time.monotonic()
                log.info("reconciliation_complete", extra={"end": end.isoformat()})
                metrics.last_reconcile.set(datetime.now(UTC).timestamp())
            metrics.pending.set(await asyncio.to_thread(store.pending_ranges))
            metrics.repair_backlog.set(0)
            health.database_ok = True
        except CapacityExceeded:
            return  # Supervisor restarts through the capacity-aware load.
        except (TransientProviderError, psycopg.OperationalError) as exc:
            log.warning("repair_retry", extra={"error_type": type(exc).__name__})
            # Reload after any failed request or commit. Cursor only advances on committed ranges.
            await asyncio.sleep(30)
            cursor = await asyncio.to_thread(store.repair_cursor, stream_key, default)
            continue
        if wake is None:
            await asyncio.sleep(settings.repair_interval_seconds)
        else:
            try:
                await asyncio.wait_for(wake.wait(), settings.repair_interval_seconds)
            except TimeoutError:
                pass
            wake.clear()


async def heartbeat(lock_connection, health: Health) -> None:
    last_log = 0.0
    while True:
        health.tick = time.monotonic()
        # Losing this session loses the advisory lock. Stop rather than risk a second owner.
        await asyncio.to_thread(lock_connection.execute, "select 1")
        if time.monotonic() - last_log >= 60:
            log.info(
                "worker_heartbeat",
                extra={
                    "connected": health.connected,
                    "session_active": health.session_active,
                    "database_ok": health.database_ok,
                },
            )
            last_log = time.monotonic()
        await asyncio.sleep(5)


async def watchlist_updates(settings, store, snapshot, reload_settings=None):
    signature = configuration_signature(settings)
    while True:
        await asyncio.sleep(settings.watchlist_poll_seconds)
        if reload_settings is not None:
            fresh = await asyncio.to_thread(reload_settings)
            if configuration_signature(fresh) != signature:
                await asyncio.to_thread(store.configure_watchlist, fresh)
                return
        current = await asyncio.to_thread(store.watchlist, snapshot.stream_key)
        if current.revision != snapshot.revision:
            return
        if settings.auto_backfill and (
            await asyncio.to_thread(store.database_bytes) >= settings.database_budget_mb * 1_000_000
        ):
            return


@asynccontextmanager
async def stream_ownership(store, stream_key, health, stop):
    """Keep probes alive while an old revision drains, without opening two streams."""
    deadline = time.monotonic() + 300
    announced = False
    with ExitStack() as stack:
        while not stop.is_set():
            health.tick = time.monotonic()
            try:
                connection = await asyncio.to_thread(
                    stack.enter_context, store.singleton(stream_key)
                )
            except StreamAlreadyOwned:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Stream ownership transfer timed out") from None
                if not announced:
                    log.info("stream_owner_wait", extra={"seconds": 300})
                    announced = True
                await asyncio.sleep(2)
                continue
            yield connection
            return
        yield None


async def run_worker(
    settings: Settings, store: PostgresStore, provider: AlpacaREST, reload_settings=None
) -> bool:
    """Return True to restart with a changed watchlist or a smaller history window."""
    health, metrics = Health(), Metrics()
    server = serve_health(health, metrics, settings.health_port)
    stop, repair_requested = asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    queue: asyncio.Queue = asyncio.Queue(maxsize=settings.queue_size)
    tasks: list[asyncio.Task] = []
    writer = None
    stopped = None
    try:
        lock_key = f"alpaca:{settings.feed}"
        async with stream_ownership(store, lock_key, health, stop) as lock_connection:
            if lock_connection is None:
                return False
            tasks.append(asyncio.create_task(heartbeat(lock_connection, health)))
            stopped = asyncio.create_task(stop.wait())
            try:
                snapshot = await asyncio.to_thread(store.configure_watchlist, settings)
                selected = Settings.model_validate(
                    {**settings.model_dump(), "symbols": list(snapshot.symbols)}
                )
                monitor = asyncio.create_task(
                    watchlist_updates(settings, store, snapshot, reload_settings)
                )
                tasks.append(monitor)
                if settings.auto_backfill:
                    loading = asyncio.create_task(
                        asyncio.to_thread(load_watchlist, provider, store, selected, snapshot)
                    )
                    tasks.append(loading)
                    done, _ = await asyncio.wait(
                        [*tasks, stopped], return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in done:
                        task.result()
                    if loading not in done or monitor in done or stop.is_set():
                        return not stop.is_set()
                    tasks.remove(loading)
                instruments = await asyncio.to_thread(
                    lambda: [provider.instrument(symbol) for symbol in selected.symbols]
                )
                series = await asyncio.to_thread(store.resolve_series, instruments, selected.feed)
                health.database_ok = True

                def connection_state(connected):
                    log.info("stream_connection", extra={"connected": connected})
                    health.connected = connected
                    metrics.connected.set(int(connected))
                    if connected:
                        repair_requested.set()

                stream = AlpacaStream(selected, on_connection=connection_state)
                cursor_key = lock_key + ":" + ",".join(sorted(selected.symbols))
                writer = asyncio.create_task(
                    write_batches(selected, store, series, queue, health, metrics)
                )
                tasks.extend(
                    [
                        asyncio.create_task(
                            collect(selected, provider, stream, queue, health, metrics)
                        ),
                        asyncio.create_task(
                            repair(
                                selected,
                                provider,
                                store,
                                series,
                                cursor_key,
                                health,
                                metrics,
                                repair_requested,
                            )
                        ),
                    ]
                )
                done, _ = await asyncio.wait(
                    [*tasks, writer, stopped], return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    task.result()
                return not stop.is_set()
            except WatchlistChanged:
                return True
            finally:
                health.stopping = True
                provider.cancel()
                for task in tasks:
                    task.cancel()
                if stopped is not None:
                    stopped.cancel()
                await asyncio.gather(
                    *tasks, *([stopped] if stopped else []), return_exceptions=True
                )
                if writer is not None and not writer.done():
                    try:
                        async with asyncio.timeout(5):
                            await queue.put(None)
                            await writer
                    except TimeoutError:
                        writer.cancel()
                        await asyncio.gather(writer, return_exceptions=True)
    finally:
        health.stopping = True
        server.shutdown()
        server.server_close()
