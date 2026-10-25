"""Resumable historical ingestion; also used by the live worker's repair loop."""

import logging
import threading
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, CancelledError, ThreadPoolExecutor, wait
from datetime import UTC, datetime, timedelta
from typing import Protocol

from .config import Settings
from .domain import NEW_YORK, Observation, Origin, TimeRange
from .ports import HistoricalBars, InstrumentLookup, TradingCalendar
from .storage import PostgresStore
from .watchlist import CapacityExceeded

log = logging.getLogger(__name__)


class HistoricalProvider(HistoricalBars, InstrumentLookup, TradingCalendar, Protocol):
    pass


def ingest_history(
    provider: HistoricalProvider,
    store: PostgresStore,
    settings: Settings,
    window: TimeRange,
    *,
    force: bool = False,
    series: dict[str, int] | None = None,
    on_complete: Callable[[TimeRange, int], None] | None = None,
    before_range: Callable[[TimeRange], None] | None = None,
    workers: int = 1,
) -> dict[str, int | str]:
    series = series or store.resolve_series(
        [provider.instrument(symbol) for symbol in settings.symbols], settings.feed
    )
    calendar = provider.calendar(
        window.start.astimezone(NEW_YORK).date(),
        (window.end - timedelta(microseconds=1)).astimezone(NEW_YORK).date(),
    )
    run_id = store.start_run("reconcile" if force else "backfill", settings.symbols, window)
    covered = set() if force else store.completed_ranges(list(series.values()), window)
    total, completed, skipped = 0, 0, 0
    cancelled = threading.Event()

    def load_chunk(chunk):
        if cancelled.is_set():
            raise CancelledError()
        if before_range:
            before_range(chunk)
        elif store.database_bytes() >= settings.database_budget_mb * 1_000_000:
            raise CapacityExceeded("Historical ingestion reached the database size budget")
        pending = (
            series
            if force
            else {
                symbol: sid
                for symbol, sid in series.items()
                if (sid, chunk.start, chunk.end) not in covered
            }
        )
        if not pending:
            return chunk, 0, True
        store.mark_pending(run_id, list(pending.values()), chunk)
        observations = []
        observed_at = datetime.now(UTC)
        for page in provider.pages(list(pending), chunk):
            if cancelled.is_set():
                raise CancelledError()
            received_at = datetime.now(UTC)
            for bar in page:
                day = calendar.get(bar.timestamp.astimezone(NEW_YORK).date())
                session = (
                    day.classify(bar.timestamp, settings.window_open, settings.window_close)
                    if day
                    else None
                )
                if session:
                    observations.append(
                        Observation(bar, session, Origin.HISTORICAL, observed_at, received_at)
                    )
        if cancelled.is_set():
            raise CancelledError()
        store.complete_range(run_id, pending, chunk, observations)
        return chunk, len(observations), False

    def observe(result):
        nonlocal total, completed, skipped
        chunk, count, was_skipped = result
        skipped += int(was_skipped)
        completed += int(not was_skipped)
        total += count
        if on_complete:
            on_complete(chunk, count)
        if not was_skipped:
            log.info(
                "historical_range_complete",
                extra={
                    "start": chunk.start.isoformat(),
                    "end": chunk.end.isoformat(),
                    "bars": count,
                    "run_id": str(run_id),
                },
            )

    try:
        if workers == 1:
            # Ordered completion is required by the incremental repair cursor.
            for chunk in window.chunks():
                observe(load_chunk(chunk))
        else:
            # Only a bounded number of day ranges may be in flight. Before any
            # capacity reset, shutdown waits for those transactions to finish.
            chunks = iter(window.chunks())
            executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="bagel-history")
            pending_futures = set()
            try:
                while True:
                    while len(pending_futures) < workers:
                        chunk = next(chunks, None)
                        if chunk is None:
                            break
                        pending_futures.add(executor.submit(load_chunk, chunk))
                    if not pending_futures:
                        break
                    done, pending_futures = wait(pending_futures, return_when=FIRST_COMPLETED)
                    for future in done:
                        observe(future.result())
            finally:
                cancelled.set()
                for future in pending_futures:
                    future.cancel()
                executor.shutdown(wait=True, cancel_futures=True)
        store.finish_run(run_id)
    except BaseException as exc:
        store.finish_run(run_id, type(exc).__name__)
        raise
    return {
        "run_id": str(run_id),
        "bars": total,
        "completed_ranges": completed,
        "skipped_ranges": skipped,
    }
