from fastapi import APIRouter, Depends

from api.routes.v1.research.route import router as research_router
from api.security import require_api_key

router = APIRouter(prefix="/v1", dependencies=[Depends(require_api_key)])
router.include_router(research_router)
