"""Durable, bounded provider jobs with transactional fencing of data writes."""

import logging
import time as monotonic_time
from collections.abc import Callable
from datetime import time, timedelta
from threading import Event, Thread
from typing import Literal, Self
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import Engine, and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from market import models as m
from market.data.collectors import MASSIVE_STOCK_FEED, Collector
from market.data.domain import TimeRange
from market.data.historical import ingest_history
from market.data.storage import PostgresStore
from market.database import database_session, row_dict
from market.events.commands import append_command
from market.research.artifacts import digest_json

log = logging.getLogger(__name__)


class LostDataJob(RuntimeError):
    pass


class HistoryJob(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: Literal["alpaca", "massive"]
    feed: str
    symbols: list[str] = Field(min_length=1)
    start: AwareDatetime
    end: AwareDatetime
    reconcile: bool = False
    import_version: str = Field(default="1", min_length=1, max_length=80)

    @field_validator("symbols")
    @classmethod
    def symbols_valid(cls, symbols: list[str]) -> list[str]:
        import re

        if any(not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,19}", s) for s in symbols):
            raise ValueError("Historical jobs need explicit stock symbols, not a streaming wildcard")
        return sorted(set(symbols))

    @model_validator(mode="after")
    def valid_window(self) -> Self:
        TimeRange(self.start, self.end)
        if (self.provider == "alpaca" and self.feed not in ("iex", "sip")) or (
            self.provider == "massive" and self.feed != MASSIVE_STOCK_FEED
        ):
            raise ValueError("Provider feed is not a supported stock aggregate capability")
        return self


class HistoryOptions(BaseModel):
    symbols: list[str]
    feed: str
    window_open: time = time(4)
    window_close: time = time(20)
    history_symbol_batch: int = 64
    batch_size: int = 500
    database_budget_mb: int = 100_000


class DataJobs:
    def __init__(self, engine: Engine):
        self.engine = engine

    def create(self, spec: HistoryJob) -> UUID:
        payload = spec.model_dump(mode="json")
        with database_session(self.engine) as session:
            statement = insert(m.DataJob).values(
                request_hash=digest_json(payload), provider=spec.provider, spec=payload
            )
            identity = session.scalar(
                statement.on_conflict_do_update(
                    index_elements=[m.DataJob.request_hash],
                    set_={"request_hash": statement.excluded.request_hash},
                ).returning(m.DataJob.id)
            )
            assert identity is not None
            append_command(session, identity, "command.data.history", spec.provider, {"job_id": str(identity)})
            return identity

    def get(self, identity: UUID) -> dict:
        with database_session(self.engine) as session:
            row = session.get(m.DataJob, identity)
            if row is None:
                raise KeyError("Data job not found")
            return row_dict(row)

    def cancel(self, identity: UUID) -> dict:
        with database_session(self.engine) as session:
            session.execute(
                update(m.DataJob)
                .where(m.DataJob.id == identity, m.DataJob.state.in_(("queued", "running")))
                .values(
                    state="canceled", lease_token=None, lease_until=None, updated_at=func.clock_timestamp()
                )
            )
        return self.get(identity)

    def retry(self, identity: UUID) -> dict:
        with database_session(self.engine) as session:
            session.execute(
                update(m.DataJob)
                .where(
                    m.DataJob.id == identity,
                    m.DataJob.state.in_(("failed", "canceled")),
                    m.DataJob.attempts < 3,
                )
                .values(state="queued", error_code=None, updated_at=func.clock_timestamp())
            )
        return self.get(identity)

    def work_once(
        self,
        dsn: str,
        provider: Collector,
        feed: str,
        stopping: Callable[[], bool] = lambda: False,
        *,
        database_budget_mb: int = 100_000,
    ) -> bool:
        with database_session(self.engine) as session:
            instance = session.scalar(
                select(m.DataJob)
                .where(
                    m.DataJob.provider == provider.source,
                    m.DataJob.spec["feed"].astext == feed,
                    or_(
                        m.DataJob.state == "queued",
                        and_(m.DataJob.state == "running", m.DataJob.lease_until <= func.clock_timestamp()),
                    ),
                )
                .order_by(m.DataJob.created_at, m.DataJob.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            claim = row_dict(instance) if instance else None
            if claim is None:
                return False
            if claim["attempts"] >= 3:
                session.execute(
                    update(m.DataJob)
                    .where(m.DataJob.id == claim["id"])
                    .values(state="failed", error_code="attempt_limit", lease_token=None, lease_until=None)
                )
                return True
            token = uuid4()
            session.execute(
                update(m.DataJob)
                .where(m.DataJob.id == claim["id"])
                .values(
                    state="running",
                    attempts=m.DataJob.attempts + 1,
                    lease_token=token,
                    lease_until=func.clock_timestamp() + timedelta(seconds=120),
                    updated_at=func.clock_timestamp(),
                )
            )

        def guard(session: Session) -> None:
            row = session.scalar(
                update(m.DataJob)
                .where(
                    m.DataJob.id == claim["id"],
                    m.DataJob.state == "running",
                    m.DataJob.lease_token == token,
                    m.DataJob.lease_until > func.clock_timestamp(),
                )
                .values(
                    lease_until=func.clock_timestamp() + timedelta(seconds=120),
                    updated_at=func.clock_timestamp(),
                )
                .returning(m.DataJob.id)
            )
            if row is None:
                raise LostDataJob("Data job claim expired or was canceled")

        finished = Event()

        def renew() -> None:
            renewed = monotonic_time.monotonic()
            while not finished.wait(2):
                if stopping():
                    provider.cancel()
                    return
                if monotonic_time.monotonic() - renewed < 20:
                    continue
                try:
                    with database_session(self.engine) as session:
                        guard(session)
                except Exception as error:
                    log.warning("data_job_lease_lost", extra={"error_type": type(error).__name__})
                    provider.cancel()
                    return
                renewed = monotonic_time.monotonic()

        heartbeat = Thread(target=renew, daemon=True)
        heartbeat.start()
        progress = {"rows_written_this_attempt": 0, "ranges_processed": 0}

        def completed(chunk: TimeRange, rows: int) -> None:
            progress["rows_written_this_attempt"] += rows
            progress["ranges_processed"] += 1
            with database_session(self.engine) as session:
                guard(session)
                session.execute(
                    update(m.DataJob)
                    .where(m.DataJob.id == claim["id"])
                    .values(result={"progress": {**progress, "complete_through": chunk.end.isoformat()}})
                )

        try:
            spec = HistoryJob.model_validate(claim["spec"])
            with PostgresStore(dsn, write_guard=guard) as store:
                result = ingest_history(
                    provider,
                    store,
                    HistoryOptions(
                        symbols=spec.symbols, feed=spec.feed, database_budget_mb=database_budget_mb
                    ),
                    TimeRange(spec.start, spec.end),
                    force=spec.reconcile,
                    on_complete=completed,
                )
            with database_session(self.engine) as session:
                guard(session)
                session.execute(
                    update(m.DataJob)
                    .where(m.DataJob.id == claim["id"])
                    .values(
                        state="complete",
                        result=result,
                        lease_token=None,
                        lease_until=None,
                        updated_at=func.clock_timestamp(),
                    )
                )
        except LostDataJob:
            log.info("data_job_interrupted", extra={"run_id": str(claim["id"])})
        except Exception as error:
            with database_session(self.engine) as session:
                session.execute(
                    update(m.DataJob)
                    .where(
                        m.DataJob.id == claim["id"],
                        m.DataJob.lease_token == token,
                        m.DataJob.lease_until > func.clock_timestamp(),
                    )
                    .values(
                        state="failed",
                        error_code=type(error).__name__,
                        lease_token=None,
                        lease_until=None,
                        updated_at=func.clock_timestamp(),
                    )
                )
            log.error(
                "data_job_failed", extra={"error_type": type(error).__name__, "run_id": str(claim["id"])}
            )
        finally:
            finished.set()
            heartbeat.join(timeout=10)
        return True
