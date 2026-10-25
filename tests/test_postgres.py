from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import psycopg
import pytest
from bagel_market_data.domain import (
    NEW_YORK,
    Instrument,
    Observation,
    Origin,
    TimeRange,
    TradingDay,
    parse_boundary,
)
from bagel_market_data.historical import ingest_history
from bagel_market_data.storage import PostgresStore
from conftest import make_bar
from psycopg import sql

from tools.migrate import LEGACY_FILENAMES, MIGRATIONS, migrate

pytestmark = pytest.mark.integration


def register(store, feed="iex", source="alpaca"):
    return store.resolve_series([Instrument("alpaca-spy", "SPY", "SPDR S&P 500")], feed, source)


def observation(origin=Origin.LIVE, seconds=0, close="100", timestamp=None):
    bar = make_bar(close=close, **({"timestamp": timestamp} if timestamp else {}))
    received = datetime(2026, 10, 22, 15, tzinfo=UTC) + timedelta(seconds=seconds)
    return Observation(bar, "regular", origin, received, received)


def test_migration_idempotency_and_private_access(database):
    admin, worker = database
    assert migrate(admin) == 0
    with psycopg.connect(worker, autocommit=True) as conn:
        assert conn.execute("select count(*) from market_data.sources").fetchone()[0] >= 0
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("create table market_data.arbitrary (id int)")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("delete from market_data.sources")
    with psycopg.connect(admin, autocommit=True) as conn:
        assert (
            conn.execute(
                "select count(*) from pg_tables where schemaname in "
                "('market_data','ingestion') and not rowsecurity"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "select has_function_privilege('public', "
                "'ingestion.ensure_bar_partitions(timestamptz,timestamptz)', "
                "'execute')"
            ).fetchone()[0]
            is False
        )


def test_duplicate_and_correction_ordering(store, database):
    series = register(store)
    first = observation()
    store.write_live(series, [first, first])
    store.write_live(series, [observation(Origin.UPDATED, 1, "101")])
    store.write_live(series, [observation(Origin.LIVE, 2, "99")])
    store.write_live(series, [observation(Origin.UPDATED, 0, "98")])
    with psycopg.connect(database[0]) as conn:
        assert conn.execute("select count(*), max(close) from market_data.bars").fetchone() == (
            1,
            Decimal("101"),
        )
    store.write_live(series, [observation(Origin.HISTORICAL, 3, "102")])
    store.write_live(series, [observation(Origin.UPDATED, 10, "100")])
    store.write_live(series, [observation(Origin.HISTORICAL, 2, "99")])
    with psycopg.connect(database[0]) as conn:
        assert conn.execute("select close, origin from market_data.bars").fetchone() == (
            Decimal("102"),
            "historical",
        )


def test_source_and_feed_isolation_with_canonical_instrument(store, database):
    iex = register(store)
    sip = register(store, "sip")
    with psycopg.connect(database[0]) as conn:
        canonical = conn.execute(
            "select instrument_id from market_data.series where id=%s", (iex["SPY"],)
        ).fetchone()[0]
    vendor = store.resolve_series(
        [Instrument("other-id", "SPY", "SPY")],
        "consolidated",
        "vendor2",
        canonical_instruments={"SPY": canonical},
    )
    for series in (iex, sip, vendor):
        store.write_live(series, [observation()])
    with psycopg.connect(database[0]) as conn:
        assert conn.execute("select count(*) from market_data.bars").fetchone()[0] == 3
        assert (
            conn.execute("select count(distinct instrument_id) from market_data.series").fetchone()[
                0
            ]
            == 1
        )


def test_partition_rollover_and_function_bounds(store, database):
    series = register(store)
    store.write_live(
        series,
        [
            observation(timestamp="2026-12-31T23:59:00Z"),
            observation(timestamp="2027-01-01T00:00:00Z"),
        ],
    )
    with psycopg.connect(database[0]) as conn:
        partitions = conn.execute(
            "select distinct tableoid::regclass::text from market_data.bars"
        ).fetchall()
        assert set(row[0] for row in partitions) == {
            "market_data.bars_2026_12",
            "market_data.bars_2027_01",
        }
    with psycopg.connect(database[1], autocommit=True) as conn:
        with pytest.raises(psycopg.Error):
            conn.execute("select ingestion.ensure_bar_partitions('2000-01-01','2030-01-01')")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("select * from market_data.bars_2027_01")


def test_singleton_lock(store, database):
    with PostgresStore(database[1]) as other, store.singleton("test:iex"):
        with pytest.raises(RuntimeError, match="owns"):
            with other.singleton("test:iex"):
                pytest.fail("second lock must not succeed")
    with store.singleton("test:iex") as connection:
        assert connection.execute("select 1").fetchone()[0] == 1


def test_symbol_reuse_and_rename(store, database):
    original = register(store)
    renamed = store.resolve_series([Instrument("alpaca-spy", "NEW", "renamed")], "iex")
    assert original["SPY"] == renamed["NEW"]
    reused = store.resolve_series([Instrument("new-security", "SPY", "new issuer")], "iex")
    assert original["SPY"] != reused["SPY"]
    with psycopg.connect(database[0]) as conn:
        assert (
            conn.execute(
                "select count(*) from market_data.instrument_mappings where valid_to is not null"
            ).fetchone()[0]
            == 1
        )


class FakeProvider:
    def __init__(self):
        self.queried = []

    def instrument(self, symbol):
        return Instrument(symbol + "-id", symbol, symbol)

    def calendar(self, start, end):
        return {
            date(2026, 10, day): TradingDay(
                date(2026, 10, day),
                datetime(2026, 10, day, 9, 30, tzinfo=NEW_YORK),
                datetime(2026, 10, day, 16, tzinfo=NEW_YORK),
            )
            for day in (21, 22)
        }

    def pages(self, symbols, window):
        self.queried.append(window)
        yield [make_bar(timestamp=f"2026-10-{window.start.day}T14:00:00Z")]


def test_transactional_resume_and_empty_symbol_coverage(store, settings, database, monkeypatch):
    settings.symbols = ["SPY", "VXUS"]
    provider = FakeProvider()
    window = TimeRange(
        parse_boundary("2026-10-21T00:00:00Z"), parse_boundary("2026-10-23T00:00:00Z")
    )
    original = store._write

    def crash_after_write(connection, series, observations):
        original(connection, series, observations)
        if observations[0].bar.timestamp.day == 22:
            raise RuntimeError("simulated crash before checkpoint")

    monkeypatch.setattr(store, "_write", crash_after_write)
    with pytest.raises(RuntimeError, match="crash"):
        ingest_history(provider, store, settings, window)
    with psycopg.connect(database[0]) as conn:
        assert conn.execute("select count(*) from market_data.bars").fetchone()[0] == 1
        assert (
            conn.execute(
                "select count(*) from ingestion.ranges where status='complete'"
            ).fetchone()[0]
            == 2
        )  # Includes VXUS's provider-confirmed empty range.
        assert (
            conn.execute("select count(*) from ingestion.ranges where status='pending'").fetchone()[
                0
            ]
            == 2
        )
    monkeypatch.setattr(store, "_write", original)
    provider.queried.clear()
    result = ingest_history(provider, store, settings, window)
    assert result["skipped_ranges"] == 1
    assert len(provider.queried) == 1 and provider.queried[0].start.day == 22
    with psycopg.connect(database[0]) as conn:
        assert conn.execute("select count(*) from market_data.bars").fetchone()[0] == 2
        assert (
            conn.execute("select count(*) from ingestion.ranges where status='pending'").fetchone()[
                0
            ]
            == 0
        )


def test_empty_holiday_is_complete_without_fabricated_bars(store, settings):
    class Holiday(FakeProvider):
        def calendar(self, *_):
            return {}

        def pages(self, *_):
            yield []

    window = TimeRange(
        parse_boundary("2026-12-25T00:00:00Z"), parse_boundary("2026-12-26T00:00:00Z")
    )
    result = ingest_history(Holiday(), store, settings, window)
    assert result["bars"] == 0
    assert store.pending_ranges() == 0


def test_watchlist_versions_preserve_database_edits(store, settings):
    initial = store.configure_watchlist(settings)
    changed = store.set_watchlist(settings.model_copy(update={"symbols": ["SPY", "QQQ"]}))
    assert changed.revision == initial.revision + 1
    assert store.configure_watchlist(settings).symbols == ("QQQ", "SPY")
    assert (
        store.set_watchlist(settings.model_copy(update={"symbols": ["QQQ", "SPY"]})).revision
        == changed.revision
    )
    fresh = settings.model_copy(update={"symbols": ["IWM"]})
    assert store.configure_watchlist(fresh).symbols == ("IWM",)


def test_direct_watchlist_edit_queues_history_and_order_changes_do_not(store, settings, database):
    initial = store.configure_watchlist(settings)
    with psycopg.connect(database[1], autocommit=True) as conn:
        conn.execute(
            "update ingestion.watchlists set symbols=array['gld',' spy ','GLD'] "
            "where stream_key='alpaca:iex'"
        )
    changed = store.watchlist(initial.stream_key)
    assert changed.symbols == ("GLD", "SPY")
    assert changed.revision == initial.revision + 1 and changed.status == "pending"
    with psycopg.connect(database[1], autocommit=True) as conn:
        conn.execute(
            "update ingestion.watchlists set symbols=array['SPY','GLD'] "
            "where stream_key='alpaca:iex'"
        )
    assert store.watchlist(initial.stream_key).revision == changed.revision


def test_capacity_reset_reclaims_bars_and_coverage_preserving_metadata(store, settings, database):
    from bagel_market_data.watchlist import WatchlistChanged

    initial = store.configure_watchlist(settings.model_copy(update={"symbols": ["SPY"]}))
    series = register(store)
    old = TimeRange(parse_boundary("2021-10-24T00:00:00Z"), parse_boundary("2026-10-24T00:00:00Z"))
    smaller = TimeRange(parse_boundary("2023-10-24T00:00:00Z"), old.end)
    assert store.begin_history(initial.stream_key, initial.revision, 5, old)
    run_id = store.start_run("backfill", ["SPY"], old)
    store.complete_range(run_id, series, old, [observation()])
    store.repair_cursor("alpaca:iex:SPY", old.end)
    with pytest.raises(psycopg.Error, match="below"):
        store.reset_history(initial.stream_key, initial.revision, 3, smaller, 10**12)
    assert not store.reset_history(initial.stream_key, initial.revision + 1, 3, smaller, 1)
    assert store.reset_history(initial.stream_key, initial.revision, 3, smaller, 1)
    with psycopg.connect(database[0]) as conn:
        for table in ("market_data.bars", "ingestion.ranges", "ingestion.stream_state"):
            assert (
                conn.execute(sql.SQL("select count(*) from {}").format(sql.SQL(table))).fetchone()[
                    0
                ]
                == 0
            )
        assert conn.execute("select count(*) from market_data.instruments").fetchone()[0] == 1
        assert conn.execute("select count(*) from ingestion.capacity_resets").fetchone()[0] == 1
    assert store.watchlist(initial.stream_key).attempt_years == 3
    with pytest.raises(WatchlistChanged, match="stale"):
        store.write_live(series, [observation(timestamp="2022-10-22T14:00:00Z")])


def test_capacity_reset_refuses_other_provider_data(store, settings):
    initial = store.configure_watchlist(settings)
    iex, sip = register(store), register(store, "sip")
    store.write_live(iex, [observation()])
    store.write_live(sip, [observation()])
    smaller = TimeRange(
        parse_boundary("2023-10-24T00:00:00Z"), parse_boundary("2026-10-24T00:00:00Z")
    )
    with pytest.raises(psycopg.Error, match="other sources"):
        store.reset_history(initial.stream_key, initial.revision, 3, smaller, 1)


def test_bulk_copy_preserves_deduplication_and_correction_order(store, database):
    series = register(store)
    initial = observation()
    batch = [
        replace(
            initial,
            bar=replace(initial.bar, timestamp=initial.bar.timestamp + timedelta(minutes=i)),
        )
        for i in range(120)
    ]
    corrected = observation(Origin.UPDATED, seconds=1, close="101")
    store.write_live(series, [*batch, corrected])
    store.write_live(series, batch)
    with psycopg.connect(database[0]) as conn:
        assert conn.execute("select count(*) from market_data.bars").fetchone()[0] == 120
        assert conn.execute(
            "select close from market_data.bars where bar_start=%s", (initial.bar.timestamp,)
        ).fetchone()[0] == Decimal("101")


def test_adding_symbol_only_requests_uncovered_symbol_ranges(store, settings):
    class Provider(FakeProvider):
        symbols_requested = []

        def pages(self, symbols, window):
            self.symbols_requested.append(list(symbols))
            yield [make_bar(timestamp="2026-10-21T14:00:00Z", symbol=symbol) for symbol in symbols]

    provider = Provider()
    window = TimeRange(
        parse_boundary("2026-10-21T00:00:00Z"), parse_boundary("2026-10-22T00:00:00Z")
    )
    settings.symbols = ["SPY"]
    ingest_history(provider, store, settings, window)
    settings.symbols = ["SPY", "VXUS"]
    ingest_history(provider, store, settings, window)
    assert provider.symbols_requested == [["SPY"], ["VXUS"]]


def test_parallel_range_failure_remains_resumable(store, settings, database, monkeypatch):
    provider = FakeProvider()
    window = TimeRange(
        parse_boundary("2026-10-21T00:00:00Z"), parse_boundary("2026-10-23T00:00:00Z")
    )
    original = store._write

    def fail_second_day(connection, series, observations):
        original(connection, series, observations)
        if observations and observations[0].bar.timestamp.day == 22:
            raise RuntimeError("parallel failure")

    monkeypatch.setattr(store, "_write", fail_second_day)
    with pytest.raises(RuntimeError, match="parallel failure"):
        ingest_history(provider, store, settings, window, workers=4)
    monkeypatch.setattr(store, "_write", original)
    ingest_history(provider, store, settings, window, workers=4)
    with psycopg.connect(database[0]) as conn:
        assert conn.execute("select count(*) from market_data.bars").fetchone()[0] == 2
    assert store.pending_ranges() == 0


def test_native_history_is_authoritative_without_checksum_cache(database):
    admin, _ = database
    filename = next(MIGRATIONS.glob("*_github_cd_smoke_test.sql")).name
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute("delete from bagel_meta.migrations where version=%s", (filename,))
    assert migrate(admin) == 0  # Native CD applied it; never replay the CREATE TABLE.


def test_verified_legacy_versions_are_adopted_without_replaying_ddl(database):
    admin, _ = database
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(
            "insert into market_data.sources(code) values ('migration-upgrade-guard') "
            "on conflict do nothing"
        )
        for new, old in LEGACY_FILENAMES.items():
            conn.execute("update bagel_meta.migrations set version=%s where version=%s", (old, new))
            conn.execute(
                "delete from supabase_migrations.schema_migrations where version=%s",
                (new.split("_", 1)[0],),
            )
    assert migrate(admin) == 0
    with psycopg.connect(admin) as conn:
        assert (
            conn.execute("select count(*) from supabase_migrations.schema_migrations").fetchone()[0]
            == 4
        )
        assert conn.execute(
            "select 1 from market_data.sources where code='migration-upgrade-guard'"
        ).fetchone()


def test_cd_marker_is_present_and_private(database):
    admin, worker = database
    with psycopg.connect(admin) as conn:
        marker = conn.execute("select marker from public.bagel_cd_smoke_test where id=1").fetchone()
        assert marker and marker[0].startswith("supabase-github-cd:")
        assert conn.execute(
            "select relrowsecurity from pg_class where oid='public.bagel_cd_smoke_test'::regclass"
        ).fetchone()[0]
    with psycopg.connect(worker, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("select * from public.bagel_cd_smoke_test")
