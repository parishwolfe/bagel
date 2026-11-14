from fastapi import APIRouter, Depends, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from api.dependencies import ResourcesDep
from api.security import require_api_key
from market.events.metrics import platform_metrics

router = APIRouter(prefix="/metrics", tags=["metrics"], dependencies=[Depends(require_api_key)])


@router.get("", response_class=Response)
def metrics(resources: ResourcesDep) -> Response:
    data = (
        platform_metrics(resources.queue.engine)
        if resources.workers.external
        else generate_latest(resources.workers.metrics.registry)
    )
    return Response(data, media_type=CONTENT_TYPE_LATEST)
