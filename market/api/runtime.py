"""Background modules owned by the single backend API lifecycle."""

import asyncio
import logging
from threading import Event

from psycopg import OperationalError
from sqlalchemy.exc import OperationalError as PoolOperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeout

from market.data.config import Settings
from market.data.observability import Health, Metrics
from market.data.providers.alpaca import AlpacaREST
from market.data.runtime import run_worker
from market.data.storage import PostgresStore
from market.database import require_local_database
from market.research.artifacts import Artifacts
from market.research.queue import Queue
from market.research.worker import work_once

log = logging.getLogger(__name__)


class BackgroundWorkers:
    def __init__(self, queue: Queue, artifacts: Artifacts, *, ingestion_enabled: bool):
        self.queue = queue
        self.artifacts = artifacts
        self.ingestion_enabled = ingestion_enabled
        self.health = Health()
        self.metrics = Metrics()
        self.stopping = Event()
        self.ingestion_stop = asyncio.Event()
        self.tasks: dict[str, asyncio.Task[None]] = {}

    def start(self) -> None:
        self.tasks["research"] = asyncio.create_task(self.research(), name="research")
        if self.ingestion_enabled:
            self.tasks["ingestion"] = asyncio.create_task(self.ingest(), name="ingestion")
        for task in self.tasks.values():
            task.add_done_callback(self.completed)

    def completed(self, task: asyncio.Task[None]) -> None:
        error = None if task.cancelled() else task.exception()
        if not self.stopping.is_set():
            log.error(
                "backend_worker_stopped",
                extra={"reason": task.get_name(), "error_type": type(error).__name__},
            )

    def running(self) -> bool:
        return bool(self.tasks) and all(not task.done() for task in self.tasks.values())

    def ready(self) -> bool:
        return self.running() and (not self.ingestion_enabled or self.health.ready)

    def status(self) -> dict[str, str]:
        research = self.tasks.get("research")
        ingestion = self.tasks.get("ingestion")
        return {
            "research": "running" if research and not research.done() else "unavailable",
            "ingestion": (
                "disabled"
                if not self.ingestion_enabled
                else "ready"
                if ingestion and not ingestion.done() and self.health.ready
                else "unavailable"
            ),
        }

    async def research(self) -> None:
        while not self.stopping.is_set():
            try:
                if await asyncio.to_thread(work_once, self.queue, self.artifacts, self.stopping.is_set):
                    continue
            except (OperationalError, PoolOperationalError, PoolTimeout) as error:
                log.warning("research_db_retry", extra={"error_type": type(error).__name__})
            await asyncio.sleep(1)

    async def ingest(self) -> None:
        restart = True
        while restart and not self.ingestion_stop.is_set():
            settings = Settings()
            settings.require_credentials()
            assert settings.database_url is not None
            require_local_database(settings.database_url.get_secret_value())
            provider = AlpacaREST(settings)
            store = PostgresStore(settings.database_url.get_secret_value())
            try:
                # Pool startup can wait for a connection; keep it off the HTTP loop.
                await asyncio.to_thread(store.ping)
                restart = await run_worker(
                    settings,
                    store,
                    provider,
                    reload_settings=Settings,
                    stop=self.ingestion_stop,
                    health=self.health,
                    metrics=self.metrics,
                )
            finally:
                try:
                    await asyncio.to_thread(store.engine.dispose)
                finally:
                    await asyncio.to_thread(provider.close)

    async def close(self) -> None:
        self.stopping.set()
        self.ingestion_stop.set()
        # Await cooperative checkpoints rather than canceling an active research thread.
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
