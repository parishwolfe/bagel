"""PostgreSQL repository. Writes and coverage checkpoints share a transaction."""

from collections import Counter
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import psycopg
from psycopg import Connection
from psycopg_pool import ConnectionPool

from .config import Settings
from .domain import Instrument, Observation, TimeRange
from .watchlist import Watchlist, WatchlistChanged, configuration_signature

UPSERT_BAR = """
insert into market_data.bars (
    series_id, bar_start, open, high, low, close, volume, vwap, trade_count,
    session, origin, observed_at, received_at
) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
on conflict (series_id, bar_start) do update set
    open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close,
    volume=excluded.volume, vwap=excluded.vwap, trade_count=excluded.trade_count,
    session=excluded.session, origin=excluded.origin, observed_at=excluded.observed_at,
    received_at=excluded.received_at, ingested_at=clock_timestamp()
where (excluded.quality_rank, excluded.observed_at)
    > (bars.quality_rank, bars.observed_at)
"""

COPY_UPSERT = """
insert into market_data.bars (
    series_id,bar_start,open,high,low,close,volume,vwap,trade_count,
    session,origin,observed_at,received_at
)
select distinct on (series_id,bar_start)
    series_id,bar_start,open,high,low,close,volume,vwap,trade_count,
    session,origin,observed_at,received_at
from pg_temp.bagel_bar_stage
order by series_id,bar_start,
    case origin when 'historical' then 30 when 'live_updated' then 20 else 10 end desc,
    observed_at desc,received_at desc
on conflict (series_id,bar_start) do update set
    open=excluded.open,high=excluded.high,low=excluded.low,close=excluded.close,
    volume=excluded.volume,vwap=excluded.vwap,trade_count=excluded.trade_count,
    session=excluded.session,origin=excluded.origin,observed_at=excluded.observed_at,
    received_at=excluded.received_at,ingested_at=clock_timestamp()
where (excluded.quality_rank,excluded.observed_at) > (bars.quality_rank,bars.observed_at)
"""


def _scalar(cursor):
    row = cursor.fetchone()
    if row is None:
        raise RuntimeError("Expected a database result row")
    return row[0]


class StreamAlreadyOwned(RuntimeError):
    """Another healthy revision still owns this feed during a rolling release."""


class PostgresStore:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.pool = ConnectionPool(
            dsn,
            min_size=1,
            max_size=4,
            timeout=30,
            kwargs={
                "autocommit": True,
                "prepare_threshold": None,
                "application_name": "bagel-market-data",
                "connect_timeout": 10,
            },
            open=False,
        )
        self._partitions: set[tuple[int, int]] = set()

    def __enter__(self):
        self.pool.open(wait=True)
        return self

    def __exit__(self, *_):
        self.pool.close()

    def ping(self) -> None:
        with self.pool.connection() as conn:
            conn.execute("select 1 from market_data.sources limit 1")

    def resolve_series(
        self,
        instruments: Sequence[Instrument],
        feed: str,
        source: str = "alpaca",
        canonical_instruments: dict[str, UUID] | None = None,
    ) -> dict[str, int]:
        result = {}
        with self.pool.connection() as conn, conn.transaction():
            # Serialize reference-data changes; never merge instruments by ticker alone.
            conn.execute("select pg_advisory_xact_lock(74021802)")
            source_id = _scalar(
                conn.execute(
                    "insert into market_data.sources(code) values (%s) "
                    "on conflict (code) do update set code=excluded.code returning id",
                    (source,),
                )
            )
            for instrument in instruments:
                row = conn.execute(
                    "select instrument_id, symbol from market_data.instrument_mappings "
                    "where source_id=%s and provider_instrument_id=%s and valid_to is null",
                    (source_id, instrument.provider_id),
                ).fetchone()
                if row:
                    instrument_id, old_symbol = row
                    if old_symbol != instrument.symbol:
                        conn.execute(
                            "update market_data.instrument_mappings set valid_to=clock_timestamp() "
                            "where source_id=%s and provider_instrument_id=%s and valid_to is null",
                            (source_id, instrument.provider_id),
                        )
                        conn.execute(
                            "insert into market_data.instrument_mappings "
                            "(source_id,provider_instrument_id,instrument_id,symbol,valid_from) "
                            "values (%s,%s,%s,%s,clock_timestamp())",
                            (source_id, instrument.provider_id, instrument_id, instrument.symbol),
                        )
                else:
                    # A reused symbol ends its old mapping; it must not inherit old bars.
                    conn.execute(
                        "update market_data.instrument_mappings set valid_to=clock_timestamp() "
                        "where source_id=%s and symbol=%s and valid_to is null",
                        (source_id, instrument.symbol),
                    )
                    instrument_id = (canonical_instruments or {}).get(instrument.symbol)
                    if instrument_id is None:
                        instrument_id = _scalar(
                            conn.execute(
                                "insert into market_data.instruments(name,asset_class,currency) "
                                "values (%s,%s,%s) returning id",
                                (instrument.name, instrument.asset_class, instrument.currency),
                            )
                        )
                    conn.execute(
                        "insert into market_data.instrument_mappings "
                        "(source_id,provider_instrument_id,instrument_id,symbol) "
                        "values (%s,%s,%s,%s)",
                        (source_id, instrument.provider_id, instrument_id, instrument.symbol),
                    )
                result[instrument.symbol] = _scalar(
                    conn.execute(
                        "insert into market_data.series "
                        "(source_id,instrument_id,feed,interval_seconds,currency,adjustment) "
                        "values (%s,%s,%s,60,%s,'raw') on conflict "
                        "(source_id,instrument_id,feed,interval_seconds,currency,adjustment) "
                        "do update set feed=excluded.feed returning id",
                        (source_id, instrument_id, feed, instrument.currency),
                    )
                )
        return result

    def start_run(self, mode: str, symbols: Sequence[str], window: TimeRange) -> UUID:
        run_id = uuid4()
        with self.pool.connection() as conn:
            conn.execute(
                "insert into ingestion.runs(id,mode,symbols,requested_start,requested_end) "
                "values (%s,%s,%s,%s,%s)",
                (run_id, mode, list(symbols), window.start, window.end),
            )
        return run_id

    def finish_run(self, run_id: UUID, error: str | None = None) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                "update ingestion.runs set status=%s,ended_at=clock_timestamp(),error_code=%s "
                "where id=%s",
                ("failed" if error else "complete", error, run_id),
            )

    def is_complete(self, series_ids: Sequence[int], window: TimeRange) -> bool:
        with self.pool.connection() as conn:
            count = _scalar(
                conn.execute(
                    "select count(*) from ingestion.ranges where series_id=any(%s) "
                    "and range_start=%s and range_end=%s and status='complete'",
                    (list(series_ids), window.start, window.end),
                )
            )
        return count == len(series_ids)

    def incomplete_series(self, series: dict[str, int], window: TimeRange) -> dict[str, int]:
        with self.pool.connection() as conn:
            completed = {
                row[0]
                for row in conn.execute(
                    "select series_id from ingestion.ranges where series_id=any(%s) "
                    "and range_start=%s and range_end=%s and status='complete'",
                    (list(series.values()), window.start, window.end),
                ).fetchall()
            }
        return {symbol: sid for symbol, sid in series.items() if sid not in completed}

    def completed_ranges(self, series_ids: Sequence[int], window: TimeRange) -> set[tuple]:
        with self.pool.connection() as conn:
            return set(
                conn.execute(
                    "select series_id,range_start,range_end from ingestion.ranges "
                    "where series_id=any(%s) and status='complete' and range_start >= %s "
                    "and range_end <= %s",
                    (list(series_ids), window.start, window.end),
                ).fetchall()
            )

    def mark_pending(self, run_id: UUID, series_ids: Sequence[int], window: TimeRange) -> None:
        with self.pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            cur.executemany(
                "insert into ingestion.ranges(series_id,range_start,range_end,run_id) "
                "values (%s,%s,%s,%s) on conflict (series_id,range_start,range_end) "
                "do nothing",
                [(sid, window.start, window.end, run_id) for sid in series_ids],
            )

    def _write(
        self, conn: Connection, series: dict[str, int], observations: Sequence[Observation]
    ) -> None:
        conn.execute("select pg_advisory_xact_lock_shared(74021803)")
        floors = dict(
            conn.execute(
                "select s.id,w.history_start from market_data.series s "
                "join market_data.sources src on src.id=s.source_id "
                "join ingestion.watchlists w on w.stream_key=src.code || ':' || s.feed "
                "where s.id=any(%s) and w.history_start is not null",
                (list(series.values()),),
            ).fetchall()
        )
        if any(
            o.bar.timestamp < floors[series[o.bar.symbol]]
            for o in observations
            if series[o.bar.symbol] in floors
        ):
            raise WatchlistChanged(
                "A stale writer attempted to write before the retained history floor"
            )
        months = {(o.bar.timestamp.year, o.bar.timestamp.month) for o in observations}
        for year, month in months - self._partitions:
            start = datetime(year, month, 1, tzinfo=UTC)
            conn.execute(
                "select ingestion.ensure_bar_partitions(%s,%s)", (start, start + timedelta(days=32))
            )
        if observations:
            with conn.cursor() as cur:
                rows = [
                    (
                        series[o.bar.symbol],
                        o.bar.timestamp,
                        o.bar.open,
                        o.bar.high,
                        o.bar.low,
                        o.bar.close,
                        o.bar.volume,
                        o.bar.vwap,
                        o.bar.trade_count,
                        o.session,
                        o.origin.value,
                        o.observed_at,
                        o.received_at,
                    )
                    for o in observations
                ]
                if len(rows) < 100:
                    cur.executemany(UPSERT_BAR, rows)
                else:
                    cur.execute(
                        "create temp table if not exists bagel_bar_stage "
                        "(like market_data.bars including defaults) on commit delete rows"
                    )
                    with cur.copy(
                        "copy pg_temp.bagel_bar_stage ("
                        "series_id,bar_start,open,high,low,close,volume,vwap,trade_count,"
                        "session,origin,observed_at,received_at) from stdin"
                    ) as copy:
                        for row in rows:
                            copy.write_row(row)
                    cur.execute(COPY_UPSERT)
        # Do not cache partition creation until the surrounding transaction has committed.

    def write_live(self, series: dict[str, int], observations: Sequence[Observation]) -> None:
        with self.pool.connection() as conn, conn.transaction():
            self._write(conn, series, observations)
        self._partitions.update((o.bar.timestamp.year, o.bar.timestamp.month) for o in observations)

    def complete_range(
        self,
        run_id: UUID,
        series: dict[str, int],
        window: TimeRange,
        observations: Sequence[Observation],
    ) -> None:
        counts = Counter(o.bar.symbol for o in observations)
        with self.pool.connection() as conn, conn.transaction():
            self._write(conn, series, observations)
            with conn.cursor() as cur:
                cur.executemany(
                    "insert into ingestion.ranges "
                    "(series_id,range_start,range_end,run_id,status,bar_count,completed_at) "
                    "values (%s,%s,%s,%s,'complete',%s,clock_timestamp()) "
                    "on conflict (series_id,range_start,range_end) do update set "
                    "status='complete',run_id=excluded.run_id,bar_count=excluded.bar_count,"
                    "completed_at=excluded.completed_at",
                    [
                        (sid, window.start, window.end, run_id, counts[symbol])
                        for symbol, sid in series.items()
                    ],
                )
        self._partitions.update((o.bar.timestamp.year, o.bar.timestamp.month) for o in observations)

    @contextmanager
    def singleton(self, stream_key: str) -> Iterator[Connection]:
        # A dedicated session, never a transaction-pooler connection.
        with psycopg.connect(
            self.dsn, autocommit=True, connect_timeout=10, application_name="bagel-stream-lock"
        ) as conn:
            acquired = _scalar(
                conn.execute("select pg_try_advisory_lock(hashtextextended(%s,0))", (stream_key,))
            )
            if not acquired:
                raise StreamAlreadyOwned("Another worker owns this source/feed stream")
            try:
                yield conn
            finally:
                if not conn.closed:
                    conn.execute("select pg_advisory_unlock(hashtextextended(%s,0))", (stream_key,))

    def repair_cursor(self, stream_key: str, default: datetime) -> datetime:
        with self.pool.connection() as conn:
            return _scalar(
                conn.execute(
                    "insert into ingestion.stream_state(stream_key,repair_cursor) values (%s,%s) "
                    "on conflict(stream_key) do update set heartbeat_at=clock_timestamp() "
                    "returning repair_cursor",
                    (stream_key, default),
                )
            )

    def advance_cursor(self, stream_key: str, cursor: datetime) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                "update ingestion.stream_state set repair_cursor=greatest(repair_cursor,%s),"
                "heartbeat_at=clock_timestamp() where stream_key=%s",
                (cursor, stream_key),
            )

    def pending_ranges(self) -> int:
        with self.pool.connection() as conn:
            return _scalar(
                conn.execute("select count(*) from ingestion.ranges where status='pending'")
            )

    def database_bytes(self) -> int:
        with self.pool.connection() as conn:
            return _scalar(conn.execute("select pg_database_size(current_database())"))

    def watchlist_capacity(self, stream_key: str) -> tuple[int, int]:
        with self.pool.connection() as conn:
            row = conn.execute(
                "select revision,pg_database_size(current_database()) "
                "from ingestion.watchlists where stream_key=%s",
                (stream_key,),
            ).fetchone()
        if row is None:
            raise WatchlistChanged("Watchlist no longer exists")
        return row[0], row[1]

    def watchlist(self, stream_key: str) -> Watchlist:
        with self.pool.connection() as conn:
            row = conn.execute(
                "select stream_key,symbols,revision,loaded_revision,attempt_years,"
                "history_start,history_end,status from ingestion.watchlists where stream_key=%s",
                (stream_key,),
            ).fetchone()
        if row is None:
            raise ValueError("Watchlist has not been initialized")
        return Watchlist(row[0], tuple(row[1]), *row[2:])

    def configure_watchlist(self, settings: Settings) -> Watchlist:
        stream_key = "alpaca:" + settings.feed
        symbols = sorted(settings.symbols)
        signature = configuration_signature(settings)
        with self.pool.connection() as conn, conn.transaction():
            conn.execute(
                "insert into ingestion.watchlists(stream_key,symbols,config_signature) "
                "values (%s,%s,%s) on conflict(stream_key) do nothing",
                (stream_key, symbols, signature),
            )
            row = conn.execute(
                "select config_signature from ingestion.watchlists where stream_key=%s for update",
                (stream_key,),
            ).fetchone()
            assert row is not None
            if row[0] is None:
                # A watchlist saved through the CLI wins over unchanged startup defaults.
                conn.execute(
                    "update ingestion.watchlists set config_signature=%s where stream_key=%s",
                    (signature, stream_key),
                )
            elif row[0] != signature:
                conn.execute(
                    "update ingestion.watchlists set symbols=%s,config_signature=%s,"
                    "revision=revision+1,attempt_years=null,status='pending',"
                    "updated_at=clock_timestamp() where stream_key=%s",
                    (symbols, signature, stream_key),
                )
        return self.watchlist(stream_key)

    def set_watchlist(self, settings: Settings) -> Watchlist:
        stream_key, symbols = "alpaca:" + settings.feed, sorted(settings.symbols)
        with self.pool.connection() as conn:
            conn.execute(
                "insert into ingestion.watchlists(stream_key,symbols) values (%s,%s) "
                "on conflict(stream_key) do update set symbols=excluded.symbols,"
                "revision=watchlists.revision+1,attempt_years=null,status='pending',"
                "updated_at=clock_timestamp() "
                "where watchlists.symbols is distinct from excluded.symbols",
                (stream_key, symbols),
            )
        return self.watchlist(stream_key)

    def begin_history(self, stream_key: str, revision: int, years: int, window: TimeRange) -> bool:
        with self.pool.connection() as conn, conn.transaction():
            conn.execute("select pg_advisory_xact_lock(74021803)")
            return (
                conn.execute(
                    "update ingestion.watchlists set attempt_years=%s,"
                    "history_start=%s,history_end=%s,status='loading',"
                    "updated_at=clock_timestamp() where stream_key=%s and revision=%s "
                    "returning revision",
                    (years, window.start, window.end, stream_key, revision),
                ).fetchone()
                is not None
            )

    def finish_history(self, stream_key: str, revision: int) -> bool:
        with self.pool.connection() as conn:
            return (
                conn.execute(
                    "update ingestion.watchlists set loaded_revision=revision,status='ready',"
                    "updated_at=clock_timestamp() where stream_key=%s and revision=%s "
                    "returning revision",
                    (stream_key, revision),
                ).fetchone()
                is not None
            )

    def fail_history(self, stream_key: str, revision: int) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                "update ingestion.watchlists set status='failed' "
                "where stream_key=%s and revision=%s",
                (stream_key, revision),
            )

    def reset_history(
        self, stream_key: str, revision: int, years: int, window: TimeRange, budget: int
    ) -> bool:
        with self.pool.connection() as conn:
            reset = _scalar(
                conn.execute(
                    "select ingestion.reset_history_for_capacity(%s,%s,%s,%s,%s,%s)",
                    (stream_key, revision, years, window.start, window.end, budget),
                )
            )
        if reset:
            self._partitions.clear()
        return reset
