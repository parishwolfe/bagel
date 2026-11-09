from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from psycopg import Error
from psycopg_pool import PoolTimeout

from market.research.artifacts import Artifacts
from market.research.config import ResearchSettings
from market.research.queue import BatchSpec, Queue, engine_id
from market.strategies.core import StrategySpec

bearer = HTTPBearer(auto_error=False)


def operator(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> UUID:
    config: ResearchSettings = request.app.state.config
    if credentials is None:
        raise HTTPException(401, "Supply a Supabase access token", {"WWW-Authenticate": "Bearer"})
    if not config.auth_key or not config.operator_ids:
        raise HTTPException(503, "Local research operators have not been configured")
    try:
        response = request.app.state.auth.get(
            "/auth/v1/user",
            headers={
                "apikey": config.auth_key.get_secret_value(),
                "Authorization": f"Bearer {credentials.credentials}",
            },
        )
    except httpx.HTTPError as error:
        raise HTTPException(503, "Local authentication unavailable") from error
    if response.status_code in (401, 403):
        raise HTTPException(401, "Invalid access token", {"WWW-Authenticate": "Bearer"})
    if response.status_code != 200:
        raise HTTPException(503, "Local authentication unavailable")
    try:
        subject = UUID(response.json()["id"])
    except (ValueError, TypeError, KeyError) as error:
        raise HTTPException(503, "Invalid authentication response") from error
    if subject not in config.operator_ids:
        raise HTTPException(403, "Research operator access required")
    return subject


Operator = Annotated[UUID, Depends(operator)]


def create_app(settings: ResearchSettings | None = None) -> FastAPI:
    config = settings or ResearchSettings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        with (
            Queue(config.database_url.get_secret_value()) as queue,
            httpx.Client(base_url=config.auth_url, timeout=5, trust_env=False) as auth,
        ):
            app.state.config = config
            app.state.queue = queue
            app.state.auth = auth
            app.state.artifacts = Artifacts(config.artifacts)
            yield

    app = FastAPI(title="Bagel Research", version="1.0.0", lifespan=lifespan)

    @app.exception_handler(KeyError)
    def missing(_: Request, exc: KeyError) -> JSONResponse:
        return JSONResponse({"detail": str(exc.args[0])}, status_code=404)

    @app.exception_handler(ValueError)
    def invalid(_: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(Error)
    @app.exception_handler(PoolTimeout)
    def unavailable(_: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"detail": "Local research database unavailable"}, status_code=503)

    @app.get("/health/live")
    def live():
        return {"ok": True}

    @app.get("/health/ready")
    def ready(request: Request):
        return {
            "ok": request.app.state.queue.ready(),
            "mode": "offline_research",
            "auth_configured": bool(config.auth_key and config.operator_ids),
        }

    @app.get("/v1/research/strategies")
    def strategies(_: Operator):
        return {"engine_id": engine_id(), "strategy_schema": StrategySpec.model_json_schema()}

    @app.get("/v1/research/datasets")
    def datasets(request: Request, _: Operator, limit: Annotated[int, Query(ge=1, le=100)] = 50):
        return request.app.state.queue.datasets(limit)

    @app.post("/v1/research/batches", status_code=202)
    def submit(request: Request, batch: BatchSpec, actor: Operator):
        return {"batch_id": request.app.state.queue.enqueue(batch, actor)}

    @app.get("/v1/research/batches/{batch_id}")
    def batch_status(request: Request, batch_id: UUID, _: Operator):
        return request.app.state.queue.batch(batch_id)

    @app.get("/v1/research/runs/{run_id}")
    def run_status(request: Request, run_id: UUID, _: Operator):
        return request.app.state.queue.run(run_id)

    @app.post("/v1/research/runs/{run_id}/cancel")
    def cancel(request: Request, run_id: UUID, _: Operator):
        return request.app.state.queue.cancel(run_id)

    @app.post("/v1/research/runs/{run_id}/retry")
    def retry(request: Request, run_id: UUID, _: Operator):
        return request.app.state.queue.retry(run_id)

    @app.get("/v1/research/runs/{run_id}/artifact")
    def artifact(request: Request, run_id: UUID, _: Operator):
        run = request.app.state.queue.run(run_id)
        if run["state"] != "succeeded":
            raise HTTPException(409, "Run does not have a completed result")
        try:
            path = request.app.state.artifacts.read(run["result_sha256"])
        except OSError as error:
            raise HTTPException(503, "Research artifact unavailable") from error
        return FileResponse(path, media_type="application/json", filename=f"{run_id}.json")

    return app
