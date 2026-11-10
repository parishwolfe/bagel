from fastapi import APIRouter, Response

from api.dependencies import ResourcesDep
from api.schemas.health import HealthResponse, ReadinessResponse, WorkerStatus

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
def live(resources: ResourcesDep, response: Response) -> HealthResponse:
    ok = resources.workers.running()
    response.status_code = 200 if ok else 503
    return HealthResponse(ok=ok)


@router.get("/ready")
def ready(resources: ResourcesDep, response: Response) -> ReadinessResponse:
    ok = resources.queue.ready() and resources.workers.ready()
    response.status_code = 200 if ok else 503
    return ReadinessResponse(
        ok=ok,
        mode="market_data" if resources.settings.ingestion_enabled else "offline_research",
        workers=WorkerStatus.model_validate(resources.workers.status()),
    )
