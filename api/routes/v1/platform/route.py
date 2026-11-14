from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import func, select

from api.dependencies import ResourcesDep
from market import models as m
from market.data.jobs import DataJobs, HistoryJob
from market.database import database_session, row_dict
from market.events.journal import Journal
from market.research.experiments import Experiments, ExperimentSpec
from market.research.queue import engine_id
from market.trading.repository import TradingRepository

router = APIRouter(tags=["platform"])


class Submitted(BaseModel):
    experiment_id: UUID


class DataJobSubmitted(BaseModel):
    job_id: UUID


@router.post("/data/jobs", status_code=202)
def submit_data_job(resources: ResourcesDep, spec: HistoryJob) -> DataJobSubmitted:
    return DataJobSubmitted(job_id=DataJobs(resources.queue.engine).create(spec))


@router.get("/data/jobs/{identity}")
def data_job(resources: ResourcesDep, identity: UUID) -> dict:
    return DataJobs(resources.queue.engine).get(identity)


@router.post("/data/jobs/{identity}/cancel")
def cancel_data_job(resources: ResourcesDep, identity: UUID) -> dict:
    return DataJobs(resources.queue.engine).cancel(identity)


@router.post("/data/jobs/{identity}/retry")
def retry_data_job(resources: ResourcesDep, identity: UUID) -> dict:
    return DataJobs(resources.queue.engine).retry(identity)


@router.get("/platform/status")
def status(resources: ResourcesDep) -> dict:
    journal = Journal(resources.queue.engine)
    with database_session(resources.queue.engine) as session:
        migrations = [
            row_dict(r) for r in session.scalars(select(m.SchemaMigration).order_by(m.SchemaMigration.name))
        ]
        extension = session.scalar(
            select(m.PostgresExtension.extversion).where(m.PostgresExtension.extname == "timescaledb")
        )
        incompatible = (
            session.scalar(
                select(func.count())
                .select_from(m.ResearchRun)
                .where(m.ResearchRun.state.in_(("queued", "running")), m.ResearchRun.engine_id != engine_id())
            )
            or 0
        )
    accounts = TradingRepository(resources.queue.engine).accounts()
    armed = any(a["submission_armed"] for a in accounts)
    return {
        "workers": journal.workers(),
        "journal": journal.backlog(),
        "migrations": migrations,
        "timescaledb": extension,
        "paper_submission_enabled": armed,
        "execution_gate": "armed" if armed else "paused_or_unreconciled",
        "research_engine_id": engine_id(),
        "research_runs_requiring_original_engine": incompatible,
    }


@router.get("/data/coverage")
def coverage(
    resources: ResourcesDep,
    after: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[dict]:
    with database_session(resources.queue.engine) as session:
        return [
            dict(r)
            for r in session.execute(
                select(
                    m.Series.id.label("series_id"),
                    m.Series.instrument_id,
                    m.Source.code.label("provider"),
                    m.Series.feed,
                    m.Series.adjustment,
                    m.IngestedUniverse.first_received_at,
                    m.IngestedUniverse.last_received_at,
                )
                .join(m.Source, m.Source.id == m.Series.source_id)
                .outerjoin(m.IngestedUniverse, m.IngestedUniverse.series_id == m.Series.id)
                .where(m.Series.id > after)
                .order_by(m.Series.id)
                .limit(limit)
            ).mappings()
        ]


@router.get("/paper/accounts")
def accounts(resources: ResourcesDep) -> list[dict]:
    with database_session(resources.queue.engine) as session:
        return [
            row_dict(r) for r in session.scalars(select(m.PaperAccount).order_by(m.PaperAccount.account_id))
        ]


@router.post("/research/experiments", status_code=202)
def submit_experiment(resources: ResourcesDep, spec: ExperimentSpec) -> Submitted:
    return Submitted(experiment_id=Experiments(resources.queue).create(spec))


@router.get("/research/experiments/{identity}")
def experiment(
    resources: ResourcesDep,
    identity: UUID,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> dict:
    return Experiments(resources.queue).get(identity, offset=offset, limit=limit)


@router.post("/research/experiments/{identity}/cancel")
def cancel_experiment(resources: ResourcesDep, identity: UUID) -> dict:
    return Experiments(resources.queue).cancel(identity)
