from fastapi import APIRouter, Depends, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from api.dependencies import ResourcesDep
from api.security import require_api_key

router = APIRouter(prefix="/metrics", tags=["metrics"], dependencies=[Depends(require_api_key)])


@router.get("", response_class=Response)
def metrics(resources: ResourcesDep) -> Response:
    return Response(generate_latest(resources.workers.metrics.registry), media_type=CONTENT_TYPE_LATEST)
