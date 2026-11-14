import json
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from api.dependencies import ResourcesDep
from api.schemas.research import (
    BatchRequest,
    BatchResponse,
    BatchSubmitted,
    DatasetResponse,
    RunDetailResponse,
    StrategyCatalog,
)
from market import models as m
from market.data.domain import TimeRange
from market.research.datasets import Digest, export_local
from market.research.queue import engine_id
from market.strategies.core import StrategySpec

router = APIRouter(prefix="/research", tags=["research"])


class DatasetExport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    series_ids: tuple[int, ...] = Field(min_length=1)
    start: AwareDatetime
    end: AwareDatetime
    row_budget: int = Field(default=1_000_000, ge=1, le=10_000_000)


@router.post("/datasets/export", status_code=201)
def export_dataset(resources: ResourcesDep, spec: DatasetExport) -> dict:
    from market.database import database_session

    window = TimeRange(spec.start, spec.end)
    with database_session(resources.queue.engine) as session:
        bounded = (
            select(m.Bar.series_id)
            .where(
                m.Bar.series_id.in_(spec.series_ids),
                m.Bar.bar_start >= window.start,
                m.Bar.bar_start < window.end,
                m.Bar.session == "regular",
            )
            .limit(spec.row_budget + 1)
            .subquery()
        )
        count = session.scalar(select(func.count()).select_from(bounded))
        if count and count > spec.row_budget:
            raise HTTPException(422, "Export exceeds the requested row resource budget; use a smaller window")
    manifest = export_local(
        resources.settings.dsn(),
        resources.artifacts,
        list(spec.series_ids),
        window,
        row_budget=spec.row_budget,
    )
    return {"dataset_id": resources.queue.register(manifest), "rows": manifest.rows}


@router.get("/strategies")
def strategies() -> StrategyCatalog:
    return StrategyCatalog(engine_id=engine_id(), strategy_schema=StrategySpec.model_json_schema())


@router.get("/engines/{identity}")
def engine_source(resources: ResourcesDep, identity: Digest) -> dict:
    return resources.queue.engine_source(identity)


@router.get("/datasets")
def datasets(
    resources: ResourcesDep, limit: Annotated[int, Query(ge=1, le=100)] = 50
) -> list[DatasetResponse]:
    return [DatasetResponse.model_validate(row) for row in resources.queue.datasets(limit)]


@router.post("/batches", status_code=202)
def submit(resources: ResourcesDep, batch: BatchRequest) -> BatchSubmitted:
    return BatchSubmitted(batch_id=resources.queue.enqueue(batch))


@router.get("/batches/{batch_id}")
def batch_status(resources: ResourcesDep, batch_id: UUID) -> BatchResponse:
    return BatchResponse.model_validate(resources.queue.batch(batch_id))


@router.get("/runs/{run_id}")
def run_status(resources: ResourcesDep, run_id: UUID) -> RunDetailResponse:
    return RunDetailResponse.model_validate(resources.queue.run(run_id))


@router.post("/runs/{run_id}/cancel")
def cancel(resources: ResourcesDep, run_id: UUID) -> RunDetailResponse:
    return RunDetailResponse.model_validate(resources.queue.cancel(run_id))


@router.post("/runs/{run_id}/retry")
def retry(resources: ResourcesDep, run_id: UUID) -> RunDetailResponse:
    return RunDetailResponse.model_validate(resources.queue.retry(run_id))


@router.get("/runs/{run_id}/artifact")
def artifact(resources: ResourcesDep, run_id: UUID) -> FileResponse:
    run = resources.queue.run(run_id)
    if run["state"] != "succeeded":
        raise HTTPException(409, "Run does not have a completed result")
    try:
        path = resources.artifacts.read(run["result_sha256"])
    except (OSError, ValueError) as error:
        raise HTTPException(503, "Research artifact unavailable") from error
    return FileResponse(path, media_type="application/json", filename=f"{run_id}.json")


@router.get("/runs/{run_id}/fills")
def fills(resources: ResourcesDep, run_id: UUID) -> FileResponse:
    run = resources.queue.run(run_id)
    if run["state"] != "succeeded":
        raise HTTPException(409, "Run does not have a completed result")
    try:
        result = json.loads(resources.artifacts.read(run["result_sha256"]).read_bytes())
        if "fill_log_sha256" not in result:
            raise HTTPException(409, "Legacy run stores fills inline in its result artifact")
        path = resources.artifacts.read(result["fill_log_sha256"])
    except (OSError, ValueError):
        raise HTTPException(503, "Research fill artifact unavailable") from None
    return FileResponse(path, media_type="application/x-ndjson", filename=f"{run_id}-fills.jsonl")
