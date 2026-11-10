from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, JsonValue

from market.backtesting.engine import BacktestSpec
from market.research.datasets import DatasetManifest, Digest
from market.research.queue import BatchSpec as BatchRequest

__all__ = [
    "BatchRequest",
    "BatchResponse",
    "BatchSubmitted",
    "DatasetResponse",
    "RunDetailResponse",
    "StrategyCatalog",
]


class StrategyCatalog(BaseModel):
    engine_id: Digest
    strategy_schema: dict[str, JsonValue]


class DatasetResponse(BaseModel):
    id: Digest
    manifest: DatasetManifest
    created_at: AwareDatetime


class BatchSubmitted(BaseModel):
    batch_id: UUID


class RunResponse(BaseModel):
    id: UUID
    dataset_id: Digest
    engine_id: Digest
    spec: BacktestSpec
    actor: UUID | None
    state: Literal["queued", "running", "succeeded", "failed", "canceled"]
    attempts: int
    result_sha256: Digest | None
    summary: dict[str, JsonValue] | None
    error_code: str | None
    created_at: AwareDatetime
    updated_at: AwareDatetime


class BatchRunResponse(RunResponse):
    ordinal: int


class BatchResponse(BaseModel):
    id: UUID
    actor: UUID | None
    created_at: AwareDatetime
    runs: list[BatchRunResponse]


class AttemptResponse(BaseModel):
    attempt: int
    state: Literal["running", "succeeded", "failed", "canceled", "expired"]
    started_at: AwareDatetime
    ended_at: AwareDatetime | None
    error_code: str | None


class RunDetailResponse(RunResponse):
    attempt_history: list[AttemptResponse]
