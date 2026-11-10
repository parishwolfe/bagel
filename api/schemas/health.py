from typing import Literal

from pydantic import BaseModel


class HealthResponse(BaseModel):
    ok: bool


class WorkerStatus(BaseModel):
    research: Literal["running", "unavailable"]
    ingestion: Literal["disabled", "ready", "unavailable"]


class ReadinessResponse(HealthResponse):
    mode: Literal["market_data", "offline_research"]
    workers: WorkerStatus
