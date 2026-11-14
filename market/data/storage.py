"""SQLAlchemy ORM market-data repository; migrations alone own schema changes."""

from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from market import models as m
from market.database import database_engine, database_session
from market.events.journal import archive

from .config import Settings
from .domain import Instrument, Observation, TimeRange
from .watchlist import Watchlist, WatchlistChanged, configuration_signature


class StreamAlreadyOwned(RuntimeError):
    pass


class PostgresStore:
    def __init__(self, dsn: str, write_guard: Callable[[Session], None] | None = None):
        self.dsn = dsn
        self.engine = database_engine(dsn, application_name="bagel-market-data", pool_size=4, timeout=10)
        self._partitions: set[tuple[int, int]] = set()
        self.write_guard = write_guard
        self._series_cache: dict[tuple[str, str, str, str], int] = {}

    def __enter__(self):
        self.ping()
        return self

    def __exit__(self, *_):
        self.engine.dispose()

    def ping(self) -> None:
        with database_session(self.engine) as session:
            session.scalar(select(m.Source.id).limit(1))

    def resolve_series(
        self,
        instruments: Sequence[Instrument],
        feed: str,
        source: str = "alpaca",
        canonical_instruments: dict[str, UUID] | None = None,
    ) -> dict[str, int]:
        cached = {
            i.symbol: self._series_cache[(source, feed, i.provider_id, i.symbol)]
            for i in instruments
            if (source, feed, i.provider_id, i.symbol) in self._series_cache
        }
        if len(cached) == len(instruments):
            return cached
        result = {}
        with database_session(self.engine) as session:
            session.scalar(select(func.pg_advisory_xact_lock(74021802)))
            statement = insert(m.Source).values(code=source)
            source_id = session.scalar(
                statement.on_conflict_do_update(
                    index_elements=[m.Source.code], set_={"code": statement.excluded.code}
                ).returning(m.Source.id)
            )
            for instrument in instruments:
                current = session.execute(
                    select(m.InstrumentMapping.instrument_id, m.InstrumentMapping.symbol).where(
                        m.InstrumentMapping.source_id == source_id,
                        m.InstrumentMapping.provider_instrument_id == instrument.provider_id,
                        m.InstrumentMapping.valid_to.is_(None),
                    )
                ).first()
                if current is not None:
                    instrument_id = current.instrument_id
                    if current.symbol == instrument.symbol:
                        need_mapping = False
                    else:
                        session.execute(
                            update(m.InstrumentMapping)
                            .where(
                                m.InstrumentMapping.source_id == source_id,
                                m.InstrumentMapping.provider_instrument_id == instrument.provider_id,
                                m.InstrumentMapping.valid_to.is_(None),
                            )
                            .values(valid_to=func.clock_timestamp())
                        )
                        need_mapping = True
                else:
                    session.execute(
                        update(m.InstrumentMapping)
                        .where(
                            m.InstrumentMapping.source_id == source_id,
                            m.InstrumentMapping.symbol == instrument.symbol,
                            m.InstrumentMapping.valid_to.is_(None),
                        )
                        .values(valid_to=func.clock_timestamp())
                    )
                    instrument_id = (canonical_instruments or {}).get(instrument.symbol)
                    if instrument_id is None:
                        instrument_id = session.scalar(
                            insert(m.Instrument)
                            .values(
                                name=instrument.name,
                                asset_class=instrument.asset_class,
                                currency=instrument.currency,
                            )
                            .returning(m.Instrument.id)
                        )
                    need_mapping = True
                if need_mapping:
                    session.execute(
                        insert(m.InstrumentMapping).values(
                            source_id=source_id,
                            provider_instrument_id=instrument.provider_id,
                            instrument_id=instrument_id,
                            symbol=instrument.symbol,
                            valid_from=func.clock_timestamp(),
                        )
                    )
                statement = insert(m.Series).values(
                    source_id=source_id,
                    instrument_id=instrument_id,
                    feed=feed,
                    interval_seconds=60,
                    currency=instrument.currency,
                    adjustment="raw",
                )
                identity = session.scalar(
                    statement.on_conflict_do_update(
                        index_elements=[
                            m.Series.source_id,
                            m.Series.instrument_id,
                            m.Series.feed,
                            m.Series.interval_seconds,
                            m.Series.currency,
                            m.Series.adjustment,
                        ],
                        set_={"feed": statement.excluded.feed},
                    ).returning(m.Series.id)
                )
                assert identity is not None
                result[instrument.symbol] = identity
        self._series_cache.update(
            {(source, feed, i.provider_id, i.symbol): result[i.symbol] for i in instruments}
        )
        return result

    def start_run(self, mode: str, symbols: Sequence[str], window: TimeRange) -> UUID:
        identity = uuid4()
        with database_session(self.engine) as session:
            session.add(
                m.IngestionRun(
                    id=identity,
                    mode=mode,
                    symbols=list(symbols),
                    requested_start=window.start,
                    requested_end=window.end,
                )
            )
        return identity

    def finish_run(self, run_id: UUID, error: str | None = None) -> None:
        with database_session(self.engine) as session:
            session.execute(
                update(m.IngestionRun)
                .where(m.IngestionRun.id == run_id)
                .values(
                    status="failed" if error else "complete", ended_at=func.clock_timestamp(), error_code=error
                )
            )

    def is_complete(self, series_ids: Sequence[int], window: TimeRange) -> bool:
        with database_session(self.engine) as session:
            count = session.scalar(
                select(func.count())
                .select_from(m.CoverageRange)
                .where(
                    m.CoverageRange.series_id.in_(series_ids),
                    m.CoverageRange.range_start == window.start,
                    m.CoverageRange.range_end == window.end,
                    m.CoverageRange.status == "complete",
                )
            )
        return count == len(series_ids)

    def incomplete_series(self, series: dict[str, int], window: TimeRange) -> dict[str, int]:
        with database_session(self.engine) as session:
            completed = set(
                session.scalars(
                    select(m.CoverageRange.series_id).where(
                        m.CoverageRange.series_id.in_(series.values()),
                        m.CoverageRange.range_start == window.start,
                        m.CoverageRange.range_end == window.end,
                        m.CoverageRange.status == "complete",
                    )
                )
            )
        return {symbol: sid for symbol, sid in series.items() if sid not in completed}

    def completed_ranges(self, series_ids: Sequence[int], window: TimeRange) -> set[tuple]:
        with database_session(self.engine) as session:
            return {
                tuple(row)
                for row in session.execute(
                    select(
                        m.CoverageRange.series_id, m.CoverageRange.range_start, m.CoverageRange.range_end
                    ).where(
                        m.CoverageRange.series_id.in_(series_ids),
                        m.CoverageRange.status == "complete",
                        m.CoverageRange.range_start >= window.start,
                        m.CoverageRange.range_end <= window.end,
                    )
                )
            }

    def mark_pending(self, run_id: UUID, series_ids: Sequence[int], window: TimeRange) -> None:
        if not series_ids:
            return
        with database_session(self.engine) as session:
            session.execute(
                insert(m.CoverageRange)
                .values(
                    [
                        dict(series_id=sid, range_start=window.start, range_end=window.end, run_id=run_id)
                        for sid in series_ids
                    ]
                )
                .on_conflict_do_nothing()
            )

    def _write(
        self,
        session: Session,
        series: dict[str, int],
        observations: Sequence[Observation],
        envelopes: list[dict] | None = None,
    ) -> None:
        if self.write_guard:
            self.write_guard(session)
        session.scalar(select(func.pg_advisory_xact_lock_shared(74021803)))
        floor_rows = session.execute(
            select(m.Series.id, m.Watchlist.history_start)
            .join(m.Source, m.Source.id == m.Series.source_id)
            .join(m.Watchlist, m.Watchlist.stream_key == m.Source.code + ":" + m.Series.feed)
            .where(m.Series.id.in_(series.values()), m.Watchlist.history_start.is_not(None))
        ).all()
        floors = {sid: floor for sid, floor in floor_rows if floor is not None}
        if any(
            o.bar.timestamp < floors[series[o.bar.symbol]]
            for o in observations
            if series[o.bar.symbol] in floors
        ):
            raise WatchlistChanged("A stale writer attempted to write before the retained history floor")
        for year, month in {
            (o.bar.timestamp.year, o.bar.timestamp.month) for o in observations
        } - self._partitions:
            start = datetime(year, month, 1, tzinfo=UTC)
            session.scalar(select(func.public.ensure_bar_partitions(start, start + timedelta(days=32))))
        if not observations:
            return
        revisions = archive(session, series, observations, envelopes=envelopes)
        # A PostgreSQL upsert cannot affect one key twice in a statement.
        latest = {}
        rank = {"historical": 30, "live_updated": 20, "live": 10}
        for observation, revision in zip(observations, revisions, strict=True):
            bar = observation.bar
            key = (series[bar.symbol], bar.timestamp)
            priority = (rank[observation.origin.value], observation.observed_at, observation.received_at)
            if key in latest and latest[key][0] >= priority:
                continue
            latest[key] = (
                priority,
                dict(
                    series_id=key[0],
                    bar_start=key[1],
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    volume=bar.volume,
                    vwap=bar.vwap,
                    trade_count=bar.trade_count,
                    session=observation.session,
                    origin=observation.origin.value,
                    observed_at=observation.observed_at,
                    received_at=observation.received_at,
                    revision_id=revision,
                ),
            )
        rows = [entry[1] for entry in latest.values()]
        for begin in range(0, len(rows), 500):
            statement = insert(m.Bar).values(rows[begin : begin + 500])
            columns = (
                "open",
                "high",
                "low",
                "close",
                "volume",
                "vwap",
                "trade_count",
                "session",
                "origin",
                "observed_at",
                "received_at",
                "revision_id",
            )
            session.execute(
                statement.on_conflict_do_update(
                    index_elements=[m.Bar.series_id, m.Bar.bar_start],
                    set_={
                        **{name: getattr(statement.excluded, name) for name in columns},
                        "ingested_at": func.clock_timestamp(),
                    },
                    where=tuple_(statement.excluded.quality_rank, statement.excluded.observed_at)
                    > tuple_(m.Bar.quality_rank, m.Bar.observed_at),
                )
            )

    def write_live(self, series: dict[str, int], observations: Sequence[Observation]) -> None:
        with database_session(self.engine) as session:
            self._write(session, series, observations)
        self._partitions.update((o.bar.timestamp.year, o.bar.timestamp.month) for o in observations)

    def complete_range(
        self, run_id: UUID, series: dict[str, int], window: TimeRange, observations: Sequence[Observation]
    ) -> None:
        with database_session(self.engine) as session:
            self._write(session, series, observations)
            self._coverage(session, run_id, series, window, Counter(o.bar.symbol for o in observations))
        self._partitions.update((o.bar.timestamp.year, o.bar.timestamp.month) for o in observations)

    @staticmethod
    def _coverage(
        session: Session, run_id: UUID, series: dict[str, int], window: TimeRange, counts: dict[str, int]
    ):
        statement = insert(m.CoverageRange).values(
            [
                dict(
                    series_id=sid,
                    range_start=window.start,
                    range_end=window.end,
                    run_id=run_id,
                    status="complete",
                    bar_count=counts.get(symbol, 0),
                    completed_at=func.clock_timestamp(),
                )
                for symbol, sid in series.items()
            ]
        )
        session.execute(
            statement.on_conflict_do_update(
                index_elements=[
                    m.CoverageRange.series_id,
                    m.CoverageRange.range_start,
                    m.CoverageRange.range_end,
                ],
                set_={
                    name: getattr(statement.excluded, name)
                    for name in ("run_id", "status", "bar_count", "completed_at")
                },
            )
        )

    def complete_coverage(
        self, run_id: UUID, series: dict[str, int], window: TimeRange, counts: dict[str, int]
    ) -> None:
        with database_session(self.engine) as session:
            if self.write_guard:
                self.write_guard(session)
            self._coverage(session, run_id, series, window, counts)

    @contextmanager
    def singleton(self, stream_key: str) -> Iterator[Session]:
        # Pin a connection across ORM commits for a PostgreSQL session-level lock.
        with self.engine.connect() as connection, Session(bind=connection) as session:
            acquired = session.scalar(select(func.pg_try_advisory_lock(func.hashtextextended(stream_key, 0))))
            session.commit()
            if not acquired:
                raise StreamAlreadyOwned("Another worker owns this source/feed stream")
            try:
                yield session
            finally:
                session.rollback()
                session.scalar(select(func.pg_advisory_unlock(func.hashtextextended(stream_key, 0))))
                session.commit()

    def repair_cursor(self, stream_key: str, default: datetime) -> datetime:
        with database_session(self.engine) as session:
            statement = insert(m.StreamState).values(stream_key=stream_key, repair_cursor=default)
            result = session.scalar(
                statement.on_conflict_do_update(
                    index_elements=[m.StreamState.stream_key], set_={"heartbeat_at": func.clock_timestamp()}
                ).returning(m.StreamState.repair_cursor)
            )
            assert result is not None
            return result

    def advance_cursor(self, stream_key: str, cursor: datetime) -> None:
        with database_session(self.engine) as session:
            session.execute(
                update(m.StreamState)
                .where(m.StreamState.stream_key == stream_key)
                .values(
                    repair_cursor=func.greatest(m.StreamState.repair_cursor, cursor),
                    heartbeat_at=func.clock_timestamp(),
                )
            )

    def pending_ranges(self) -> int:
        with database_session(self.engine) as session:
            return (
                session.scalar(
                    select(func.count())
                    .select_from(m.CoverageRange)
                    .where(m.CoverageRange.status == "pending")
                )
                or 0
            )

    def database_bytes(self) -> int:
        with database_session(self.engine) as session:
            return session.scalar(select(func.pg_database_size(func.current_database()))) or 0

    def watchlist_capacity(self, stream_key: str) -> tuple[int, int]:
        with database_session(self.engine) as session:
            row = session.execute(
                select(m.Watchlist.revision, func.pg_database_size(func.current_database())).where(
                    m.Watchlist.stream_key == stream_key
                )
            ).first()
            if row is None:
                raise WatchlistChanged("Watchlist no longer exists")
            return row[0], row[1]

    def watchlist(self, stream_key: str) -> Watchlist:
        with database_session(self.engine) as session:
            row = session.get(m.Watchlist, stream_key)
            if row is None:
                raise ValueError("Watchlist has not been initialized")
            return Watchlist(
                row.stream_key,
                tuple(row.symbols),
                row.revision,
                row.loaded_revision,
                row.attempt_years,
                row.history_start,
                row.history_end,
                row.status,
            )

    def configure_watchlist(self, settings: Settings) -> Watchlist:
        key, symbols, signature = (
            "alpaca:" + settings.feed,
            sorted(settings.symbols),
            configuration_signature(settings),
        )
        with database_session(self.engine) as session:
            session.execute(
                insert(m.Watchlist)
                .values(stream_key=key, symbols=symbols, config_signature=signature)
                .on_conflict_do_nothing()
            )
            row = session.scalar(select(m.Watchlist).where(m.Watchlist.stream_key == key).with_for_update())
            assert row is not None
            if row.config_signature is None:
                row.config_signature = signature
            elif row.config_signature != signature:
                row.symbols, row.config_signature, row.attempt_years, row.status = (
                    symbols,
                    signature,
                    None,
                    "pending",
                )
                row.updated_at = datetime.now(UTC)
        return self.watchlist(key)

    def set_watchlist(self, settings: Settings) -> Watchlist:
        key, symbols = "alpaca:" + settings.feed, sorted(settings.symbols)
        with database_session(self.engine) as session:
            statement = insert(m.Watchlist).values(stream_key=key, symbols=symbols)
            session.execute(
                statement.on_conflict_do_update(
                    index_elements=[m.Watchlist.stream_key],
                    set_={
                        "symbols": symbols,
                        "attempt_years": None,
                        "status": "pending",
                        "updated_at": func.clock_timestamp(),
                    },
                    where=m.Watchlist.symbols.is_distinct_from(statement.excluded.symbols),
                )
            )
        return self.watchlist(key)

    def begin_history(self, stream_key: str, revision: int, years: int, window: TimeRange) -> bool:
        with database_session(self.engine) as session:
            session.scalar(select(func.pg_advisory_xact_lock(74021803)))
            return (
                session.scalar(
                    update(m.Watchlist)
                    .where(m.Watchlist.stream_key == stream_key, m.Watchlist.revision == revision)
                    .values(
                        attempt_years=years,
                        history_start=window.start,
                        history_end=window.end,
                        status="loading",
                        updated_at=func.clock_timestamp(),
                    )
                    .returning(m.Watchlist.revision)
                )
                is not None
            )

    def finish_history(self, stream_key: str, revision: int) -> bool:
        with database_session(self.engine) as session:
            return (
                session.scalar(
                    update(m.Watchlist)
                    .where(m.Watchlist.stream_key == stream_key, m.Watchlist.revision == revision)
                    .values(
                        loaded_revision=m.Watchlist.revision, status="ready", updated_at=func.clock_timestamp()
                    )
                    .returning(m.Watchlist.revision)
                )
                is not None
            )

    def fail_history(self, stream_key: str, revision: int) -> None:
        with database_session(self.engine) as session:
            session.execute(
                update(m.Watchlist)
                .where(m.Watchlist.stream_key == stream_key, m.Watchlist.revision == revision)
                .values(status="failed")
            )

    def reset_history(
        self, stream_key: str, revision: int, years: int, window: TimeRange, budget: int
    ) -> bool:
        with database_session(self.engine) as session:
            reset = bool(
                session.scalar(
                    select(
                        func.public.reset_history_for_capacity(
                            stream_key, revision, years, window.start, window.end, budget
                        )
                    )
                )
            )
        if reset:
            self._partitions.clear()
        return reset
