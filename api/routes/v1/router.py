from fastapi import APIRouter, Depends

from api.routes.v1.platform.route import router as platform_router
from api.routes.v1.research.route import router as research_router
from api.routes.v1.strategies.route import router as strategies_router
from api.routes.v1.trading.route import router as trading_router
from api.security import require_api_key

router = APIRouter(prefix="/v1", dependencies=[Depends(require_api_key)])
router.include_router(research_router)
router.include_router(platform_router)
router.include_router(trading_router)
router.include_router(strategies_router)
