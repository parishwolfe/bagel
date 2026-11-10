import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from psycopg import Error
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from api.dependencies import Resources
from api.runtime import BackgroundWorkers
from api.settings import Settings
from market.data.observability import configure_logging
from market.research.artifacts import Artifacts
from market.research.queue import Queue


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging("INFO")
    try:
        config = app.state.settings or Settings()
    except ValidationError as error:
        fields = sorted({".".join(map(str, item["loc"])) for item in error.errors()})
        raise RuntimeError("Invalid API configuration: " + ", ".join(fields)) from None
    configure_logging(config.log_level)

    try:
        queue = Queue(config.dsn())
        try:
            artifacts = await asyncio.to_thread(Artifacts, config.artifacts)
            workers = BackgroundWorkers(queue, artifacts, ingestion_enabled=config.ingestion_enabled)
            app.state.resources = Resources(config, queue, artifacts, workers)
            try:
                workers.start()
                yield
            finally:
                await workers.close()
        finally:
            await asyncio.to_thread(queue.engine.dispose)
    except (Error, SQLAlchemyError, OSError) as error:
        raise RuntimeError(f"API resources unavailable ({type(error).__name__})") from None
    finally:
        app.state.resources = None
