from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from psycopg import Error
from sqlalchemy.exc import SQLAlchemyError
from starlette.responses import Response

from market.api.runtime import BackgroundWorkers
from market.research.artifacts import Artifacts
from market.research.config import ResearchSettings
from market.research.queue import BatchSpec, Queue, engine_id
from market.strategies.core import StrategySpec


def create_app(settings: ResearchSettings | None = None, *, ingestion_enabled: bool = False) -> FastAPI:
    config = settings or ResearchSettings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        with Queue(config.dsn()) as queue:
            app.state.queue = queue
            app.state.artifacts = Artifacts(config.artifacts)
            workers = BackgroundWorkers(queue, app.state.artifacts, ingestion_enabled=ingestion_enabled)
            app.state.workers = workers
            workers.start()
            try:
                yield
            finally:
                await workers.close()

    app = FastAPI(title="Bagel Backend", version="1.0.0", lifespan=lifespan)

    @app.exception_handler(KeyError)
    def missing(_: Request, exc: KeyError) -> JSONResponse:
        return JSONResponse({"detail": str(exc.args[0])}, status_code=404)

    @app.exception_handler(ValueError)
    def invalid(_: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(Error)
    @app.exception_handler(SQLAlchemyError)
    def unavailable(_: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"detail": "Local research database unavailable"}, status_code=503)

    @app.get("/health/live")
    def live(request: Request):
        ok = request.app.state.workers.running()
        return JSONResponse({"ok": ok}, status_code=200 if ok else 503)

    @app.get("/health/ready")
    def ready(request: Request):
        workers: BackgroundWorkers = request.app.state.workers
        ok = request.app.state.queue.ready() and workers.ready()
        return JSONResponse(
            {
                "ok": ok,
                "mode": "market_data" if ingestion_enabled else "offline_research",
                "workers": workers.status(),
            },
            status_code=200 if ok else 503,
        )

    @app.get("/metrics")
    def metrics(request: Request):
        workers: BackgroundWorkers = request.app.state.workers
        return Response(generate_latest(workers.metrics.registry), media_type=CONTENT_TYPE_LATEST)

    @app.get("/v1/research/strategies")
    def strategies():
        return {"engine_id": engine_id(), "strategy_schema": StrategySpec.model_json_schema()}

    @app.get("/v1/research/datasets")
    def datasets(request: Request, limit: Annotated[int, Query(ge=1, le=100)] = 50):
        return request.app.state.queue.datasets(limit)

    @app.post("/v1/research/batches", status_code=202)
    def submit(request: Request, batch: BatchSpec):
        return {"batch_id": request.app.state.queue.enqueue(batch)}

    @app.get("/v1/research/batches/{batch_id}")
    def batch_status(request: Request, batch_id: UUID):
        return request.app.state.queue.batch(batch_id)

    @app.get("/v1/research/runs/{run_id}")
    def run_status(request: Request, run_id: UUID):
        return request.app.state.queue.run(run_id)

    @app.post("/v1/research/runs/{run_id}/cancel")
    def cancel(request: Request, run_id: UUID):
        return request.app.state.queue.cancel(run_id)

    @app.post("/v1/research/runs/{run_id}/retry")
    def retry(request: Request, run_id: UUID):
        return request.app.state.queue.retry(run_id)

    @app.get("/v1/research/runs/{run_id}/artifact")
    def artifact(request: Request, run_id: UUID):
        run = request.app.state.queue.run(run_id)
        if run["state"] != "succeeded":
            raise HTTPException(409, "Run does not have a completed result")
        try:
            path = request.app.state.artifacts.read(run["result_sha256"])
        except OSError as error:
            raise HTTPException(503, "Research artifact unavailable") from error
        return FileResponse(path, media_type="application/json", filename=f"{run_id}.json")

    return app
