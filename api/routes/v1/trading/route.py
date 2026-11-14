from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel
from pydantic_core import to_jsonable_python

from api.dependencies import ResourcesDep
from market.trading.contracts import ArmCommand, CancelCommand, OrderCommand
from market.trading.repository import CommandConflict, TradingRepository

router = APIRouter(prefix="/trading", tags=["trading"])
IdempotencyKey = Annotated[UUID | None, Header(alias="Idempotency-Key")]


class TradingSubmitted(BaseModel):
    command_id: UUID


def submit(
    resources: ResourcesDep, account_id: UUID, action: str, payload: dict, key: UUID | None
) -> TradingSubmitted:
    try:
        identity = TradingRepository(resources.queue.engine).submit(account_id, action, payload, key)
    except CommandConflict:
        raise HTTPException(409, "Idempotency key was already used for another command") from None
    return TradingSubmitted(command_id=identity)


@router.get("/accounts")
def accounts(resources: ResourcesDep) -> list[dict]:
    return to_jsonable_python(TradingRepository(resources.queue.engine).accounts())


@router.post("/accounts/{account_id}/arm", status_code=202)
def arm(
    resources: ResourcesDep, account_id: UUID, command: ArmCommand, idempotency_key: IdempotencyKey = None
) -> TradingSubmitted:
    return submit(resources, account_id, "arm", command.model_dump(mode="json"), idempotency_key)


@router.post("/accounts/{account_id}/pause", status_code=202)
def pause(
    resources: ResourcesDep, account_id: UUID, idempotency_key: IdempotencyKey = None
) -> TradingSubmitted:
    return submit(resources, account_id, "pause", {}, idempotency_key)


@router.post("/accounts/{account_id}/orders", status_code=202)
def order(
    resources: ResourcesDep, account_id: UUID, command: OrderCommand, idempotency_key: IdempotencyKey = None
) -> TradingSubmitted:
    return submit(resources, account_id, "order", command.model_dump(mode="json"), idempotency_key)


@router.post("/accounts/{account_id}/cancel", status_code=202)
def cancel(
    resources: ResourcesDep, account_id: UUID, command: CancelCommand, idempotency_key: IdempotencyKey = None
) -> TradingSubmitted:
    return submit(resources, account_id, "cancel", command.model_dump(mode="json"), idempotency_key)


@router.post("/accounts/{account_id}/reconcile", status_code=202)
def reconcile(
    resources: ResourcesDep, account_id: UUID, idempotency_key: IdempotencyKey = None
) -> TradingSubmitted:
    return submit(resources, account_id, "reconcile", {}, idempotency_key)


@router.get("/commands/{identity}")
def command(resources: ResourcesDep, identity: UUID) -> dict:
    return to_jsonable_python(TradingRepository(resources.queue.engine).command(identity))


@router.get("/accounts/{account_id}/orders")
def orders(
    resources: ResourcesDep,
    account_id: UUID,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[dict]:
    return to_jsonable_python(TradingRepository(resources.queue.engine).orders(account_id, offset, limit))


@router.get("/accounts/{account_id}/risk")
def risk(
    resources: ResourcesDep,
    account_id: UUID,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[dict]:
    return to_jsonable_python(TradingRepository(resources.queue.engine).risk(account_id, offset, limit))


@router.get("/accounts/{account_id}/audit")
def audit(
    resources: ResourcesDep,
    account_id: UUID,
    after: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[dict]:
    return to_jsonable_python(TradingRepository(resources.queue.engine).audit(account_id, after, limit))
