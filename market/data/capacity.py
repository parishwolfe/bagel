"""Load the longest requested history that fits, without exhausting the free tier."""

import logging

import psycopg

from .config import Settings
from .domain import TimeRange, subtract_years
from .historical import ingest_history
from .watchlist import CapacityExceeded, HistoryDoesNotFit, Watchlist, WatchlistChanged

log = logging.getLogger(__name__)


def history_options(settings: Settings, requested: TimeRange) -> list[tuple[int, TimeRange]]:
    years = [settings.max_history_years, *[y for y in (3, 2, 1) if y < settings.max_history_years]]
    options = []
    starts = set()
    for year in years:
        start = max(requested.start, subtract_years(requested.end, year))
        if start not in starts:
            options.append((year, TimeRange(start, requested.end)))
            starts.add(start)
    return options


def load_watchlist(provider, store, settings: Settings, watchlist: Watchlist):
    """One caller per feed; interrupted attempts resume their committed day ranges."""
    budget = settings.database_budget_mb * 1_000_000
    if watchlist.loaded_revision == watchlist.revision and store.database_bytes() < budget:
        return {"status": "unchanged", "years": watchlist.attempt_years}
    selected = Settings.model_validate(
        {**settings.model_dump(), "symbols": list(watchlist.symbols)}
    )
    requested = selected.history_window()
    options = history_options(selected, requested)
    if watchlist.attempt_years is not None:
        options = [(years, window) for years, window in options if years <= watchlist.attempt_years]
    # A separate lease prevents a manual load and a worker startup racing this policy.
    with store.singleton("history-loader:" + watchlist.stream_key):
        for index, (years, window) in enumerate(options):
            if not store.begin_history(watchlist.stream_key, watchlist.revision, years, window):
                raise WatchlistChanged("Watchlist changed before loading")

            def check_capacity(*_):
                revision, database_bytes = store.watchlist_capacity(watchlist.stream_key)
                if revision != watchlist.revision:
                    raise WatchlistChanged("Watchlist changed during loading")
                if database_bytes >= budget:
                    raise CapacityExceeded("Market history reached the configured database budget")

            try:
                check_capacity()
                result = ingest_history(
                    provider,
                    store,
                    selected,
                    window,
                    before_range=check_capacity,
                    on_complete=check_capacity,
                    workers=settings.backfill_workers,
                )
                check_capacity()
                if not store.finish_history(watchlist.stream_key, watchlist.revision):
                    raise WatchlistChanged("Watchlist changed before load completion")
                return {**result, "status": "ready", "years": years}
            except (psycopg.errors.DiskFull, psycopg.errors.ReadOnlySqlTransaction):
                # Never truncate for a permission/network error or unrelated read-only setting.
                if store.database_bytes() < budget:
                    raise
            except CapacityExceeded:
                pass
            if index + 1 == len(options):
                store.fail_history(watchlist.stream_key, watchlist.revision)
                raise HistoryDoesNotFit(
                    "Even one year does not fit; reduce the watchlist or add capacity"
                )
            next_years, next_window = options[index + 1]
            log.warning("history_capacity_fallback", extra={"years": next_years})
            if not store.reset_history(
                watchlist.stream_key, watchlist.revision, next_years, next_window, budget
            ):
                raise WatchlistChanged("Watchlist changed before capacity reset")
    raise HistoryDoesNotFit("No historical range is available for this configuration")
