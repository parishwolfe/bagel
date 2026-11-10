import hashlib
import platform
from dataclasses import dataclass
from functools import cache
from importlib.metadata import version
from pathlib import Path
from uuid import UUID, uuid4

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from market.backtesting.engine import ENGINE_VERSION, BacktestSpec
from market.database import database_connection, database_engine, require_local_database
from market.research.artifacts import digest_json
from market.research.datasets import DatasetManifest, Digest

LEASE_SECONDS = 120
MAX_ATTEMPTS = 3


class BatchSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_id: Digest
    runs: tuple[BacktestSpec, ...] = Field(min_length=1, max_length=1000)


@cache
def engine_id() -> str:
    root = Path(__file__).resolve().parents[1]
    files = (
        "backtesting/engine.py",
        "data/domain.py",
        "execution/simulator.py",
        "portfolio/ledger.py",
        "research/datasets.py",
        "risk/policy.py",
        "strategies/core.py",
    )
    return digest_json(
        {
            "engine": ENGINE_VERSION,
            "python": platform.python_version(),
            "dependencies": {name: version(name) for name in ("pydantic", "pyarrow")},
            "files": {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in files},
        }
    )


@dataclass(frozen=True)
class Claim:
    run_id: UUID
    token: UUID
    dataset_id: str
    spec: dict


RUN_COLUMNS = """r.id, r.dataset_id, r.engine_id, r.spec, r.actor, r.state, r.attempts,
    r.result_sha256, r.summary, r.error_code, r.created_at, r.updated_at"""


class Queue:
    def __init__(self, dsn: str):
        require_local_database(dsn)
        self.engine = database_engine(dsn, application_name="bagel-research", pool_size=6, timeout=5)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.engine.dispose()

    def ready(self) -> bool:
        with database_connection(self.engine, row_factory=dict_row) as conn:
            conn.execute("select 1 from public.research_runs limit 1").fetchone()
            return True

    def register(self, manifest: DatasetManifest) -> str:
        with database_connection(self.engine, row_factory=dict_row) as conn:
            conn.execute(
                "insert into public.datasets (id, manifest) values (%s, %s) on conflict do nothing",
                (manifest.id, Jsonb(manifest.model_dump(mode="json"))),
            )
        return manifest.id

    def datasets(self, limit: int = 100) -> list[dict]:
        with database_connection(self.engine, row_factory=dict_row) as conn:
            return conn.execute(
                "select id, manifest, created_at from public.datasets order by created_at desc, id limit %s",
                (limit,),
            ).fetchall()

    def dataset(self, dataset_id: str) -> DatasetManifest:
        with database_connection(self.engine, row_factory=dict_row) as conn:
            row = conn.execute("select manifest from public.datasets where id = %s", (dataset_id,)).fetchone()
        if row is None:
            raise KeyError("Dataset not found")
        manifest = DatasetManifest.model_validate(row["manifest"])
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
        with database_connection(self.engine, row_factory=dict_row) as conn:
            row = conn.execute(
                """insert into public.batches (request_hash, actor) values (%s, %s)
                   on conflict do nothing returning id""",
                (batch_hash, actor),
            ).fetchone()
            if row is None:
                existing = conn.execute(
                    "select id from public.batches where request_hash = %s", (batch_hash,)
                ).fetchone()
                assert existing is not None
                return existing["id"]
            batch_id = row["id"]
            # Consistent insert order prevents overlapping concurrent batches from deadlocking.
            run_ids = {}
            for request_hash, spec in sorted(zip(run_hashes, specs, strict=True), key=lambda x: x[0]):
                if request_hash in run_ids:
                    continue
                conn.execute(
                    """insert into public.research_runs (request_hash, dataset_id, engine_id, spec, actor)
                       values (%s, %s, %s, %s, %s) on conflict do nothing""",
                    (request_hash, batch.dataset_id, engine, Jsonb(spec), actor),
                )
                run = conn.execute(
                    "select id from public.research_runs where request_hash = %s", (request_hash,)
                ).fetchone()
                assert run is not None
                run_ids[request_hash] = run["id"]
            with conn.cursor() as cur:
                cur.executemany(
                    "insert into public.batch_runs (batch_id, ordinal, run_id) values (%s, %s, %s)",
                    [(batch_id, i, run_ids[key]) for i, key in enumerate(run_hashes)],
                )
        return batch_id

    def batch(self, batch_id: UUID) -> dict:
        with database_connection(self.engine, row_factory=dict_row) as conn:
            batch = conn.execute(
                "select id, actor, created_at from public.batches where id = %s", (batch_id,)
            ).fetchone()
            if batch is None:
                raise KeyError("Batch not found")
            batch["runs"] = conn.execute(
                f"""select b.ordinal, {RUN_COLUMNS} from public.batch_runs b
                      join public.research_runs r on r.id = b.run_id
                     where b.batch_id = %s order by b.ordinal""",
                (batch_id,),
            ).fetchall()
            return batch

    def run(self, run_id: UUID) -> dict:
        with database_connection(self.engine, row_factory=dict_row) as conn:
            row = conn.execute(
                f"select {RUN_COLUMNS} from public.research_runs r where r.id = %s", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError("Run not found")
            row["attempt_history"] = conn.execute(
                """select attempt, state, started_at, ended_at, error_code from public.attempts
                   where run_id = %s order by attempt""",
                (run_id,),
            ).fetchall()
            return row

    def claim(self) -> Claim | None:
        with database_connection(self.engine, row_factory=dict_row) as conn:
            while True:
                row = conn.execute(
                    """select id, dataset_id, spec, attempts, lease_token from public.research_runs
                       where engine_id = %s and (state = 'queued' or
                         (state = 'running' and lease_until <= clock_timestamp()))
                       order by created_at, id limit 1 for update skip locked""",
                    (engine_id(),),
                ).fetchone()
                if row is None:
                    return None
                if row["lease_token"]:
                    self._end_attempt(conn, row["lease_token"], "expired", "lease_expired")
                if row["attempts"] >= MAX_ATTEMPTS:
                    conn.execute(
                        """update public.research_runs set state = 'failed', error_code = 'attempt_limit',
                           lease_token = null, lease_until = null, updated_at = clock_timestamp()
                           where id = %s""",
                        (row["id"],),
                    )
                    continue
                token = uuid4()
                conn.execute(
                    """update public.research_runs set state = 'running', attempts = attempts + 1,
                       lease_token = %s, lease_until = clock_timestamp() + %s * interval '1 second',
                       updated_at = clock_timestamp(), error_code = null where id = %s""",
                    (token, LEASE_SECONDS, row["id"]),
                )
                conn.execute(
                    """insert into public.attempts (run_id, attempt, token, state)
                       values (%s, %s, %s, 'running')""",
                    (row["id"], row["attempts"] + 1, token),
                )
                return Claim(row["id"], token, row["dataset_id"], row["spec"])

    def renew(self, claim: Claim) -> bool:
        with database_connection(self.engine, row_factory=dict_row) as conn:
            return (
                conn.execute(
                    """update public.research_runs
                   set lease_until = clock_timestamp() + %s * interval '1 second',
                   updated_at = clock_timestamp()
                   where id = %s and state = 'running' and lease_token = %s
                     and lease_until > clock_timestamp()""",
                    (LEASE_SECONDS, claim.run_id, claim.token),
                ).rowcount
                == 1
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
        with database_connection(self.engine, row_factory=dict_row) as conn:
            count = conn.execute(
                """update public.research_runs set state = %s, result_sha256 = %s, summary = %s,
                   error_code = %s, lease_token = null, lease_until = null,
                   updated_at = clock_timestamp()
                   where id = %s and state = 'running' and lease_token = %s
                     and lease_until > clock_timestamp()""",
                (
                    state,
                    result,
                    Jsonb(summary) if summary is not None else None,
                    error,
                    claim.run_id,
                    claim.token,
                ),
            ).rowcount
            if count:
                self._end_attempt(conn, claim.token, state, error)
            return count == 1

    def cancel(self, run_id: UUID) -> dict:
        with database_connection(self.engine, row_factory=dict_row) as conn:
            row = conn.execute(
                "select state, lease_token from public.research_runs where id = %s for update",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError("Run not found")
            if row["state"] in ("queued", "running"):
                conn.execute(
                    """update public.research_runs set state = 'canceled', lease_token = null,
                       lease_until = null, updated_at = clock_timestamp() where id = %s""",
                    (run_id,),
                )
                if row["lease_token"]:
                    self._end_attempt(conn, row["lease_token"], "canceled")
        return self.run(run_id)

    def retry(self, run_id: UUID) -> dict:
        with database_connection(self.engine, row_factory=dict_row) as conn:
            count = conn.execute(
                """update public.research_runs set state = 'queued', error_code = null,
                   updated_at = clock_timestamp() where id = %s and state in ('failed', 'canceled')
                   and attempts < %s and engine_id = %s""",
                (run_id, MAX_ATTEMPTS, engine_id()),
            ).rowcount
        row = self.run(run_id)
        if not count and row["state"] not in ("queued", "running"):
            raise ValueError("Run cannot be retried with this engine or attempt budget")
        return row

    @staticmethod
    def _end_attempt(conn: Connection, token: UUID, state: str, error: str | None = None):
        conn.execute(
            """update public.attempts
               set state = %s, error_code = %s, ended_at = clock_timestamp()
               where token = %s and state = 'running'""",
            (state, error, token),
        )
