from contextlib import nullcontext
from dataclasses import replace

import psycopg
import pytest
from bagel_market_data.capacity import load_watchlist
from bagel_market_data.watchlist import (
    CapacityExceeded,
    HistoryDoesNotFit,
    Watchlist,
    WatchlistChanged,
    configuration_signature,
)


class CapacityStore:
    def __init__(self):
        self.current = Watchlist("alpaca:iex", ("SPY",), 1, None, None, None, None, "pending")
        self.used = 0
        self.resets = []
        self.attempts = []

    def database_bytes(self):
        return self.used

    def watchlist_capacity(self, _):
        return self.current.revision, self.used

    def singleton(self, _):
        return nullcontext()

    def begin_history(self, key, revision, years, window):
        self.attempts.append(years)
        self.current = replace(
            self.current,
            attempt_years=years,
            history_start=window.start,
            history_end=window.end,
            status="loading",
        )
        return True

    def watchlist(self, _):
        return self.current

    def reset_history(self, key, revision, years, window, budget):
        assert self.used >= budget
        self.resets.append(years)
        self.used = 0
        self.current = replace(self.current, attempt_years=years, history_start=window.start)
        return True

    def finish_history(self, *_):
        self.current = replace(self.current, loaded_revision=1, status="ready")
        return True

    def fail_history(self, *_):
        self.current = replace(self.current, status="failed")


def test_capacity_fallback_tries_5_3_2_and_preserves_success(settings, monkeypatch):
    store = CapacityStore()

    def ingest(*args, **kwargs):
        if store.current.attempt_years > 2:
            store.used = settings.database_budget_mb * 1_000_000
            kwargs["on_complete"]()
        return {"bars": 100}

    monkeypatch.setattr("bagel_market_data.capacity.ingest_history", ingest)
    result = load_watchlist(None, store, settings, store.current)
    assert result["years"] == 2
    assert store.attempts == [5, 3, 2] and store.resets == [3, 2]
    assert store.current.status == "ready"
    assert load_watchlist(None, store, settings, store.current)["status"] == "unchanged"


def test_one_year_failure_stops_instead_of_repeated_truncation(settings, monkeypatch):
    store = CapacityStore()

    def ingest(*_, **kwargs):
        store.used = settings.database_budget_mb * 1_000_000
        raise CapacityExceeded("full")

    monkeypatch.setattr("bagel_market_data.capacity.ingest_history", ingest)
    with pytest.raises(HistoryDoesNotFit, match="one year"):
        load_watchlist(None, store, settings, store.current)
    assert store.attempts == [5, 3, 2, 1]
    assert store.resets == [3, 2, 1]
    assert store.current.status == "failed"


def test_interrupted_three_year_attempt_resumes_without_retrying_five(settings, monkeypatch):
    store = CapacityStore()
    store.current = replace(store.current, attempt_years=3)
    monkeypatch.setattr("bagel_market_data.capacity.ingest_history", lambda *_, **__: {"bars": 1})
    assert load_watchlist(None, store, settings, store.current)["years"] == 3
    assert store.attempts == [3] and not store.resets


def test_permission_failure_never_truncates(settings, monkeypatch):
    store = CapacityStore()

    def denied(*_, **__):
        raise psycopg.errors.InsufficientPrivilege("no access")

    monkeypatch.setattr("bagel_market_data.capacity.ingest_history", denied)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        load_watchlist(None, store, settings, store.current)
    assert not store.resets


def test_changed_watchlist_does_not_reset_old_revision(settings, monkeypatch):
    store = CapacityStore()
    requested = store.current
    store.current = replace(store.current, revision=2)
    with pytest.raises(WatchlistChanged):
        load_watchlist(None, store, settings, requested)
    assert not store.resets


def test_symbol_order_does_not_change_configuration(settings):
    reordered = settings.model_copy(update={"symbols": list(reversed(settings.symbols))})
    assert configuration_signature(settings) == configuration_signature(reordered)
