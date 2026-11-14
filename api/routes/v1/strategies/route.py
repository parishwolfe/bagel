from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import BaseModel
from pydantic_core import to_jsonable_python

from api.dependencies import ResourcesDep
from market.strategies.runtime import REGISTRY
from market.strategies.service import Strategies, StrategyRunSpec

router = APIRouter(prefix="/strategies", tags=["strategies"])


class StrategySubmitted(BaseModel):
    strategy_id: UUID


@router.get("/catalog")
def catalog() -> dict:
    return {"registry": REGISTRY, "run_schema": StrategyRunSpec.model_json_schema()}


@router.post("/runs", status_code=202)
def create(resources: ResourcesDep, spec: StrategyRunSpec) -> StrategySubmitted:
    return StrategySubmitted(strategy_id=Strategies(resources.queue.engine).create(spec))


@router.get("/runs")
def runs(
    resources: ResourcesDep,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[dict]:
    return to_jsonable_python(Strategies(resources.queue.engine).runs(offset, limit))


@router.get("/runs/{identity}")
def run(resources: ResourcesDep, identity: UUID) -> dict:
    return to_jsonable_python(Strategies(resources.queue.engine).get(identity))


@router.post("/runs/{identity}/start")
def start(resources: ResourcesDep, identity: UUID) -> dict:
    return to_jsonable_python(Strategies(resources.queue.engine).control(identity, True))


@router.post("/runs/{identity}/pause")
def pause(resources: ResourcesDep, identity: UUID) -> dict:
    return to_jsonable_python(Strategies(resources.queue.engine).control(identity, False))


@router.get("/runs/{identity}/signals")
def signals(
    resources: ResourcesDep,
    identity: UUID,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[dict]:
    return to_jsonable_python(Strategies(resources.queue.engine).signals(identity, offset, limit))
