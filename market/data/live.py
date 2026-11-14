"""Thin live collector: provider transport -> bounded normalization -> Redis."""

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from threading import Event
from uuid import uuid4

from redis.exceptions import RedisError

from market.data.config import Settings
from market.data.domain import Observation
from market.data.observability import Health, Metrics
from market.data.providers.alpaca import AlpacaREST, AlpacaStream, TransientProviderError
from market.data.schedule import CollectionWindow, collection_window
from market.events.bus import MarketBus
from market.events.contracts import market_event

log = logging.getLogger(__name__)


async def run_live(
    settings: Settings,
    stop: Event,
    *,
    bus: MarketBus | None = None,
    provider=None,
    stream=None,
    health: Health | None = None,
    metrics: Metrics | None = None,
) -> None:
    bus = bus or MarketBus.from_settings()
    provider = provider or AlpacaREST(settings)

    def connection_state(value: bool) -> None:
        nonlocal connected
        connected = value
        if metrics:
            metrics.connected.set(value)

    stream = stream or AlpacaStream(settings, on_connection=connection_state)
    identity = str(uuid4())
    queue: asyncio.Queue = asyncio.Queue(maxsize=settings.queue_size)
    instruments = {}
    connected = False
    publish_blocked = False
    status_reason = "starting"
    active = False
    lock = bus.redis.lock(
        bus.settings.market_stream + ":owner:market:" + settings.feed, timeout=30, thread_local=False
    )
    acquired = False

    async def collect_session(status: CollectionWindow) -> None:
        async def read() -> None:
            async for bar, origin in stream.events(settings.symbols):
                connection_state(True)
                received = datetime.now(UTC)
                session = (
                    status.day.classify(bar.timestamp, settings.window_open, settings.window_close)
                    if status.day
                    else None
                )
                if session:
                    if bar.timestamp + timedelta(minutes=1) > received:
                        raise ValueError("Provider emitted an uncompleted minute")
                    # Await bounded capacity, rather than discard a received bar on timeout.
                    await queue.put(Observation(bar, session, origin, received, received))
                    if metrics:
                        metrics.received.inc()
                if stop.is_set():
                    return
            raise TransientProviderError("Collector stream ended")

        async def gate() -> None:
            nonlocal status, status_reason, active
            while not stop.is_set():
                await asyncio.sleep(30)
                status = await asyncio.to_thread(collection_window, settings, provider, datetime.now(UTC))
                status_reason, active = status.reason, status.active
                if not status.allowed:
                    return

        reader, monitor = asyncio.create_task(read()), asyncio.create_task(gate())
        try:
            done, _ = await asyncio.wait((reader, monitor), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            reader.cancel()
            monitor.cancel()
            await asyncio.gather(reader, monitor, return_exceptions=True)
            connection_state(False)

    async def receive() -> None:
        nonlocal status_reason, active
        failures = 0
        while not stop.is_set():
            try:
                status = await asyncio.to_thread(collection_window, settings, provider, datetime.now(UTC))
                status_reason, active = status.reason, status.active
                if not status.allowed:
                    await asyncio.sleep(5)
                    continue
                await collect_session(status)
                failures = 0
            except (TransientProviderError, TimeoutError, OSError) as error:
                connection_state(False)
                failures += 1
                if metrics:
                    metrics.reconnects.inc()
                log.warning("live_collector_retry", extra={"error_type": type(error).__name__})
                await asyncio.sleep(min(30, 2 ** min(failures, 5)))

    async def publish() -> None:
        nonlocal publish_blocked
        while not stop.is_set() or not queue.empty():
            try:
                observation = await asyncio.wait_for(queue.get(), 1)
            except TimeoutError:
                continue
            symbol = observation.bar.symbol
            # Dynamic wildcard members resolve through provider reference data, never SQL.
            if symbol not in instruments:
                raise ValueError("Live symbol is absent from the preloaded provider reference catalogue")
            event = market_event(
                observation,
                provider="alpaca",
                feed=settings.feed,
                provider_instrument_id=instruments[symbol].provider_id,
            )
            while True:
                try:
                    await asyncio.to_thread(bus.publish, event)
                    publish_blocked = False
                    queue.task_done()
                    if metrics:
                        metrics.published.inc()
                        metrics.lag.set((datetime.now(UTC) - event.payload.end).total_seconds())
                    break
                except RedisError as error:
                    publish_blocked = True
                    if metrics:
                        metrics.publish_failures.inc()
                    log.error(
                        "market_publish_blocked",
                        extra={"error_type": type(error).__name__, "event_id": str(event.event_id)},
                    )
                    if stop.is_set():
                        raise
                    await asyncio.sleep(1)

    async def monitor_health() -> None:
        while not stop.is_set():
            ready = not publish_blocked and (not active or connected)
            if health_state:
                health_state.tick = time.monotonic()
                health_state.transport_ok = not publish_blocked
                health_state.connected, health_state.session_active = connected, active
            if metrics:
                metrics.queue_depth.set(queue.qsize())
            await asyncio.to_thread(
                bus.heartbeat,
                "market",
                identity,
                "ready" if ready else "blocked",
                {
                    "stream_enabled": True,
                    "stream_connected": connected,
                    "queue_depth": queue.qsize(),
                    "transport": "redis_first",
                    "reason": "redis_publish_blocked" if publish_blocked else status_reason,
                },
            )
            await asyncio.to_thread(bus.safe_trim)
            await asyncio.sleep(2)

    async def renew() -> None:
        while not stop.is_set():
            await asyncio.sleep(5)
            await asyncio.to_thread(lock.extend, 30, replace_ttl=True)

    health_state = health
    if health_state:
        health_state.stopping = False
        health_state.stream_enabled = settings.stream_enabled
    try:
        if not settings.stream_enabled:
            while not stop.is_set():
                await asyncio.to_thread(
                    bus.heartbeat,
                    "market",
                    identity,
                    "disabled",
                    {"reason": "stream_disabled", "stream_enabled": False},
                )
                if health_state:
                    health_state.tick, health_state.transport_ok = time.monotonic(), True
                await asyncio.sleep(2)
            return
        while not stop.is_set():
            acquired = await asyncio.to_thread(lock.acquire, blocking=False)
            if acquired:
                break
            await asyncio.to_thread(
                bus.heartbeat,
                "market",
                identity,
                "blocked",
                {"reason": "waiting_for_owner", "stream_enabled": True},
            )
            await asyncio.sleep(1)
        if not acquired:
            return
        if settings.symbols == ["*"]:
            instruments.update(
                {instrument.symbol: instrument for instrument in await asyncio.to_thread(provider.instruments)}
            )
        else:
            for symbol in settings.symbols:
                instruments[symbol] = await asyncio.to_thread(provider.instrument, symbol)
        reader = asyncio.create_task(receive())
        writer = asyncio.create_task(publish())
        monitor = asyncio.create_task(monitor_health())
        ownership = asyncio.create_task(renew())
        stopped = asyncio.create_task(asyncio.to_thread(stop.wait))
        tasks = (reader, writer, monitor, ownership, stopped)
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            if stop.is_set():
                reader.cancel()
                monitor.cancel()
                async with asyncio.timeout(30):
                    await writer
        finally:
            stop.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        if health_state:
            health_state.stopping = True
        if acquired:
            try:
                await asyncio.to_thread(lock.release)
            except RedisError as error:
                log.warning("collector_ownership_release_failed", extra={"error_type": type(error).__name__})
        provider.close()
        bus.close()
