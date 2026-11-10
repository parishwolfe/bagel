from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from api.dependencies import ResourcesDep
from api.schemas.research import (
    BatchRequest,
    BatchResponse,
    BatchSubmitted,
    DatasetResponse,
    RunDetailResponse,
    StrategyCatalog,
)
from market.research.queue import engine_id
from market.strategies.core import StrategySpec

router = APIRouter(prefix="/research", tags=["research"])


@router.get("/strategies")
def strategies() -> StrategyCatalog:
    return StrategyCatalog(engine_id=engine_id(), strategy_schema=StrategySpec.model_json_schema())


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
