import hashlib
import platform
from dataclasses import dataclass
from datetime import timedelta
from functools import cache
from importlib.metadata import version
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from market import models as m
from market.backtesting.engine import ENGINE_VERSION, BacktestSpec
from market.database import database_engine, database_session, require_local_database, row_dict
from market.events.commands import append_command
from market.research.artifacts import digest_json
from market.research.datasets import DatasetManifest, Digest

LEASE_SECONDS = 120
MAX_ATTEMPTS = 3


class BatchSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_id: Digest
    runs: tuple[BacktestSpec, ...] = Field(min_length=1, max_length=1000)


@cache
def engine_bundle() -> dict:
    root = Path(__file__).resolve().parents[1]
    files = (
        "backtesting/engine.py",
        "data/domain.py",
        "execution/simulator.py",
        "portfolio/ledger.py",
        "portfolio/targets.py",
        "research/datasets.py",
        "research/worker.py",
        "research/experiments.py",
        "research/artifacts.py",
        "research/queue.py",
        "risk/policy.py",
        "strategies/core.py",
        "strategies/runtime.py",
    )
    sources = {name: (root / name).read_text() for name in files}
    return {
        "metadata": {
            "engine": ENGINE_VERSION,
            "python": platform.python_version(),
            "dependencies": {name: version(name) for name in ("pydantic", "pyarrow")},
            "files": {name: hashlib.sha256(source.encode()).hexdigest() for name, source in sources.items()},
        },
        "sources": sources,
    }


@cache
def engine_id() -> str:
    return digest_json(engine_bundle()["metadata"])


@dataclass(frozen=True)
class Claim:
    run_id: UUID
    token: UUID
    dataset_id: str
    spec: dict


class Queue:
    def __init__(self, dsn: str):
        require_local_database(dsn)
        self.engine = database_engine(dsn, application_name="bagel-research", pool_size=6, timeout=5)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.engine.dispose()

    def ready(self) -> bool:
        with database_session(self.engine) as session:
            session.scalar(select(m.ResearchRun.id).limit(1))
            return True

    def register(self, manifest: DatasetManifest) -> str:
        with database_session(self.engine) as session:
            session.execute(
                insert(m.Dataset)
                .values(id=manifest.id, manifest=manifest.model_dump(mode="json"))
                .on_conflict_do_nothing()
            )
        return manifest.id

    def datasets(self, limit: int = 100) -> list[dict]:
        with database_session(self.engine) as session:
            return [
                row_dict(row)
                for row in session.scalars(
                    select(m.Dataset).order_by(m.Dataset.created_at.desc(), m.Dataset.id).limit(limit)
                )
            ]

    def dataset(self, dataset_id: str) -> DatasetManifest:
        with database_session(self.engine) as session:
            row = session.get(m.Dataset, dataset_id)
        if row is None:
            raise KeyError("Dataset not found")
        manifest = DatasetManifest.model_validate(row.manifest)
        if manifest.id != dataset_id:
            raise ValueError("Dataset manifest checksum mismatch")
        return manifest

    def enqueue(self, batch: BatchSpec, actor: UUID | None = None) -> UUID:
        manifest = self.dataset(batch.dataset_id)
        available = {series.instrument for series in manifest.series}
        for spec in batch.runs:
            selected = set(spec.instruments) or available
            if not selected <= available or not set(spec.borrow_instruments) <= selected:
                raise ValueError("Run instruments and borrow assumptions must belong to the dataset")
            if spec.start and not manifest.first_bar <= spec.start < manifest.end_exclusive:
                raise ValueError("Run start is outside the dataset")
            if spec.end and not manifest.first_bar < spec.end <= manifest.end_exclusive:
                raise ValueError("Run end is outside the dataset")
        engine = engine_id()
        specs = [spec.model_dump(mode="json") for spec in batch.runs]
        run_hashes = [digest_json((batch.dataset_id, engine, spec)) for spec in specs]
        batch_hash = digest_json(run_hashes)
        with database_session(self.engine) as session:
            if session.get(m.ResearchEngine, engine) is None:
                bundle = engine_bundle()
                session.execute(
                    insert(m.ResearchEngine)
                    .values(
                        id=engine,
                        metadata_=bundle["metadata"],
                        sources=bundle["sources"],
                        source_sha256=digest_json(bundle["sources"]),
                    )
                    .on_conflict_do_nothing()
                )
            batch_id = session.scalar(
                insert(m.Batch)
                .values(request_hash=batch_hash, actor=actor)
                .on_conflict_do_nothing()
                .returning(m.Batch.id)
            )
            if batch_id is None:
                existing = session.scalar(select(m.Batch.id).where(m.Batch.request_hash == batch_hash))
                assert existing is not None
                append_command(
                    session, existing, "command.research.batch", batch.dataset_id, {"batch_id": str(existing)}
                )
                return existing
            append_command(
                session, batch_id, "command.research.batch", batch.dataset_id, {"batch_id": str(batch_id)}
            )
            # Consistent insert order prevents overlapping concurrent batches from deadlocking.
            run_ids = {}
            for request_hash, spec in sorted(zip(run_hashes, specs, strict=True), key=lambda x: x[0]):
                if request_hash in run_ids:
                    continue
                session.execute(
                    insert(m.ResearchRun)
                    .values(
                        request_hash=request_hash,
                        dataset_id=batch.dataset_id,
                        engine_id=engine,
                        spec=spec,
                        actor=actor,
                    )
                    .on_conflict_do_nothing()
                )
                run = session.scalar(
                    select(m.ResearchRun.id).where(m.ResearchRun.request_hash == request_hash)
                )
                assert run is not None
                run_ids[request_hash] = run
            session.execute(
                insert(m.BatchRun),
                [dict(batch_id=batch_id, ordinal=i, run_id=run_ids[key]) for i, key in enumerate(run_hashes)],
            )
        return batch_id

    def batch(self, batch_id: UUID) -> dict:
        with database_session(self.engine) as session:
            batch = session.get(m.Batch, batch_id)
            if batch is None:
                raise KeyError("Batch not found")
            return {
                **row_dict(batch),
                "runs": [
                    {**row_dict(run), "ordinal": ordinal}
                    for run, ordinal in session.execute(
                        select(m.ResearchRun, m.BatchRun.ordinal)
                        .join(m.BatchRun, m.BatchRun.run_id == m.ResearchRun.id)
                        .where(m.BatchRun.batch_id == batch_id)
                        .order_by(m.BatchRun.ordinal)
                    )
                ],
            }

    def run(self, run_id: UUID) -> dict:
        with database_session(self.engine) as session:
            row = session.get(m.ResearchRun, run_id)
            if row is None:
                raise KeyError("Run not found")
            return {
                **row_dict(row),
                "attempt_history": [
                    row_dict(a)
                    for a in session.scalars(
                        select(m.Attempt).where(m.Attempt.run_id == run_id).order_by(m.Attempt.attempt)
                    )
                ],
            }

    def engine_source(self, identity: str) -> dict:
        with database_session(self.engine) as session:
            instance = session.get(m.ResearchEngine, identity)
            row = row_dict(instance) if instance else None
        if row is None:
            raise KeyError("Engine source was not archived for this legacy run")
        if digest_json(row["metadata"]) != identity or digest_json(row["sources"]) != row["source_sha256"]:
            raise ValueError("Archived engine source checksum mismatch")
        if any(
            hashlib.sha256(row["sources"].get(name, "").encode()).hexdigest() != checksum
            for name, checksum in row["metadata"]["files"].items()
        ):
            raise ValueError("Archived engine file checksum mismatch")
        return row

    def claim(self) -> Claim | None:
        with database_session(self.engine) as session:
            while True:
                row = session.scalar(
                    select(m.ResearchRun)
                    .where(
                        m.ResearchRun.engine_id == engine_id(),
                        or_(
                            m.ResearchRun.state == "queued",
                            and_(
                                m.ResearchRun.state == "running",
                                m.ResearchRun.lease_until <= func.clock_timestamp(),
                            ),
                        ),
                    )
                    .order_by(m.ResearchRun.created_at, m.ResearchRun.id)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
                if row is None:
                    return None
                if row.lease_token:
                    self._end_attempt(session, row.lease_token, "expired", "lease_expired")
                if row.attempts >= MAX_ATTEMPTS:
                    row.state, row.error_code, row.lease_token, row.lease_until = (
                        "failed",
                        "attempt_limit",
                        None,
                        None,
                    )
                    session.flush()
                    continue
                token = uuid4()
                row.state, row.attempts, row.lease_token = "running", row.attempts + 1, token
                now = session.scalar(select(func.clock_timestamp()))
                assert now is not None
                row.lease_until = now + timedelta(seconds=LEASE_SECONDS)
                row.updated_at = now
                row.error_code = None
                session.add(m.Attempt(run_id=row.id, attempt=row.attempts, token=token, state="running"))
                return Claim(row.id, token, row.dataset_id, row.spec)

    def renew(self, claim: Claim) -> bool:
        with database_session(self.engine) as session:
            return (
                session.scalar(
                    update(m.ResearchRun)
                    .where(
                        m.ResearchRun.id == claim.run_id,
                        m.ResearchRun.state == "running",
                        m.ResearchRun.lease_token == claim.token,
                        m.ResearchRun.lease_until > func.clock_timestamp(),
                    )
                    .values(
                        lease_until=func.clock_timestamp() + timedelta(seconds=LEASE_SECONDS),
                        updated_at=func.clock_timestamp(),
                    )
                    .returning(m.ResearchRun.id)
                )
                is not None
            )

    def finish(
        self,
        claim: Claim,
        *,
        result: str | None = None,
        summary: dict | None = None,
        error: str | None = None,
    ) -> bool:
        state = "succeeded" if result is not None else "failed"
        with database_session(self.engine) as session:
            count = (
                session.scalar(
                    update(m.ResearchRun)
                    .where(
                        m.ResearchRun.id == claim.run_id,
                        m.ResearchRun.state == "running",
                        m.ResearchRun.lease_token == claim.token,
                        m.ResearchRun.lease_until > func.clock_timestamp(),
                    )
                    .values(
                        state=state,
                        result_sha256=result,
                        summary=summary,
                        error_code=error,
                        lease_token=None,
                        lease_until=None,
                        updated_at=func.clock_timestamp(),
                    )
                    .returning(m.ResearchRun.id)
                )
                is not None
            )
            if count:
                self._end_attempt(session, claim.token, state, error)
            return count

    def cancel(self, run_id: UUID) -> dict:
        with database_session(self.engine) as session:
            row = session.scalar(select(m.ResearchRun).where(m.ResearchRun.id == run_id).with_for_update())
            if row is None:
                raise KeyError("Run not found")
            if row.state in ("queued", "running"):
                token = row.lease_token
                row.state, row.lease_token, row.lease_until = "canceled", None, None
                if token:
                    self._end_attempt(session, token, "canceled")
        return self.run(run_id)

    def retry(self, run_id: UUID) -> dict:
        with database_session(self.engine) as session:
            count = (
                session.scalar(
                    update(m.ResearchRun)
                    .where(
                        m.ResearchRun.id == run_id,
                        m.ResearchRun.state.in_(("failed", "canceled")),
                        m.ResearchRun.attempts < MAX_ATTEMPTS,
                        m.ResearchRun.engine_id == engine_id(),
                    )
                    .values(state="queued", error_code=None, updated_at=func.clock_timestamp())
                    .returning(m.ResearchRun.id)
                )
                is not None
            )
        row = self.run(run_id)
        if not count and row["state"] not in ("queued", "running"):
            raise ValueError("Run cannot be retried with this engine or attempt budget")
        return row

    @staticmethod
    def _end_attempt(session: Session, token: UUID, state: str, error: str | None = None):
        session.execute(
            update(m.Attempt)
            .where(m.Attempt.token == token, m.Attempt.state == "running")
            .values(state=state, error_code=error, ended_at=func.clock_timestamp())
        )
