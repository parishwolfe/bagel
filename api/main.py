from fastapi import FastAPI

from api.exceptions import register_exception_handlers
from api.lifespan import lifespan
from api.routes.health.route import router as health_router
from api.routes.metrics.route import router as metrics_router
from api.routes.middleware import NoStoreMiddleware
from api.routes.v1.router import router as v1_router
from api.settings import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    application = FastAPI(title="Bagel Control API", version="1.0.0", lifespan=lifespan)
    application.state.settings = settings
    application.add_middleware(NoStoreMiddleware)
    register_exception_handlers(application)
    application.include_router(health_router)
    application.include_router(metrics_router)
    application.include_router(v1_router)
    return application


app = create_app()
