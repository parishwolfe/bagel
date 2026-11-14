"""Separate Redis-first collection, strategy, ORM persistence and account processes."""

import argparse
import asyncio
import logging
import multiprocessing
import signal
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from threading import Event, Thread
from uuid import UUID, uuid4

from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError

from market.data.config import Settings as DataSettings
from market.data.domain import TimeRange
from market.data.historical import ingest_history
from market.data.jobs import DataJobs
from market.data.live import run_live
from market.data.observability import configure_logging
from market.data.providers.alpaca import AlpacaREST
from market.data.storage import PostgresStore
from market.events.bus import MarketBus
from market.events.config import EventSettings
from market.events.consumer import persist_decisions_once, persist_once
from market.events.journal import Journal
from market.execution.account import run_account
from market.execution.alpaca import PaperSettings
from market.execution.data_relay import RelaySettings, serve_relay
from market.research.artifacts import Artifacts
from market.research.experiments import Experiments
from market.research.queue import Queue
from market.research.worker import work_once
from market.strategies.worker import StrategyWorker

log = logging.getLogger(__name__)


def research_step(dsn: str, path: str) -> bool:
    with Queue(dsn) as queue:
        return work_once(queue, Artifacts(Path(path)))


def run_research(settings: EventSettings, stop: Event, journal: Journal) -> None:
    identity = uuid4()
    with ProcessPoolExecutor(
        max_workers=settings.research_processes, mp_context=multiprocessing.get_context("spawn")
    ) as executor:
        futures = set()
        while not stop.is_set():
            with Queue(settings.dsn()) as queue:
                Experiments(queue, settings.research_pending_budget).expand_once()
            for future in tuple(futures):
                if future.done():
                    futures.remove(future)
                    future.result()
            while len(futures) < settings.research_processes:
                futures.add(executor.submit(research_step, settings.dsn(), str(settings.artifacts)))
            journal.heartbeat("research", identity, "ready", {"processes": settings.research_processes})
            stop.wait(1)


def run_history(settings: EventSettings, stop: Event, journal: Journal) -> None:
    config = DataSettings()
    identity = uuid4()
    provider = AlpacaREST(config)
    next_repair = 0.0
    try:
        with PostgresStore(settings.dsn()) as store:
            while not stop.is_set():
                worked = DataJobs(store.engine).work_once(
                    settings.dsn(),
                    provider,
                    config.feed,
                    stop.is_set,
                    database_budget_mb=config.database_budget_mb,
                )
                if config.integration_enabled and time.monotonic() >= next_repair and config.symbols != ["*"]:
                    end = config.eligible_end()
                    key = "alpaca:" + config.feed + ":" + ",".join(sorted(config.symbols))
                    cursor = store.repair_cursor(key, end)
                    if cursor < end:
                        ingest_history(
                            provider,
                            store,
                            config,
                            TimeRange(cursor, end),
                            on_complete=lambda chunk, count, cursor_key=key: store.advance_cursor(
                                cursor_key, chunk.end
                            ),
                        )
                    next_repair = time.monotonic() + config.repair_interval_seconds
                journal.heartbeat("history", identity, "ready", {"worked": worked, "feed": config.feed})
                stop.wait(1)
    finally:
        provider.close()


def run_massive(settings: EventSettings, stop: Event, journal: Journal) -> None:
    from market.data.providers.massive import FEED, MassiveREST, MassiveSettings

    config = MassiveSettings()
    identity = uuid4()
    provider = MassiveREST(config)
    try:
        while not stop.is_set():
            enabled = bool(config.api_key and config.api_key.get_secret_value().strip())
            journal.heartbeat(
                "massive",
                identity,
                "ready" if enabled else "disabled",
                {
                    "feed": FEED,
                    "reason": "configured" if enabled else "credentials_missing",
                    "requests_per_minute": config.requests_per_minute,
                },
            )
            if enabled:
                worked = DataJobs(journal.engine).work_once(
                    settings.dsn(), provider, FEED, stop.is_set, database_budget_mb=config.database_budget_mb
                )
                if worked:
                    last = provider.last_request
                    provider.close()
                    provider = MassiveREST(config)
                    provider.last_request = last
            stop.wait(1)
    finally:
        provider.close()


def run_strategy(settings: EventSettings, stop: Event, bus: MarketBus) -> None:
    engine = Queue(settings.dsn()).engine
    identity = uuid4()
    worker = StrategyWorker(engine, identity, bus)
    try:
        while not stop.is_set():
            worked = worker.step()
            bus.heartbeat(
                "strategy", str(identity), "ready", {"worked": worked, "transport": "redis_payloads"}
            )
            stop.wait(0.02 if worked else 0.2)
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "role",
        choices=(
            "publisher",
            "research",
            "market",
            "history",
            "execution",
            "massive",
            "consumer",
            "strategy",
            "relay",
        ),
    )
    role = parser.parse_args().role
    configure_logging("INFO")
    stop = Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    bus = MarketBus.from_settings()
    identity = str(uuid4())
    lock = None
    watchdog = None
    try:
        if role in ("execution", "history"):
            scope = role
            lock = bus.redis.lock("bagel:owner:v2:" + role + ":" + scope, timeout=30, thread_local=False)
            while not stop.is_set() and not lock.acquire(blocking=False):
                bus.heartbeat(role, identity, "blocked", {"reason": "waiting_for_owner"})
                stop.wait(1)
            if stop.is_set():
                return

            def renew() -> None:
                while not stop.wait(5):
                    try:
                        lock.extend(30, replace_ttl=True)
                    except Exception as error:
                        log.error("worker_ownership_lost", extra={"error_type": type(error).__name__})
                        stop.set()
                        return

            watchdog = Thread(target=renew, daemon=True)
            watchdog.start()
        if role == "relay":
            asyncio.run(serve_relay(RelaySettings(), stop))
            return
        if role == "market":
            config = DataSettings()
            if config.integration_enabled:
                asyncio.run(run_live(config, stop, bus=bus))
            else:
                while not stop.is_set():
                    bus.heartbeat(
                        "market",
                        identity,
                        "disabled",
                        {"reason": "ingestion_disabled", "stream_enabled": False},
                    )
                    stop.wait(2)
            return
        settings = EventSettings()
        if role == "strategy":
            run_strategy(settings, stop, bus)
            return
        with PostgresStore(settings.dsn()) as store:
            journal = Journal(store.engine)
            if role == "execution":
                asyncio.run(run_account(PaperSettings(), journal, stop.is_set))
            elif role == "research":
                run_research(settings, stop, journal)
            elif role == "massive":
                run_massive(settings, stop, journal)
            elif role == "history":
                run_history(settings, stop, journal)
            else:
                while not stop.is_set():
                    try:
                        if role == "consumer":
                            count = persist_once(
                                bus, store, identity, database_budget_mb=settings.database_budget_mb
                            )
                            count += persist_decisions_once(bus, store, identity)
                            bus.heartbeat(
                                "consumer",
                                identity,
                                "ready",
                                {"last_batch": count, "transport": "redis_to_orm"},
                            )
                        else:
                            count = journal.publish(
                                bus.redis, settings.publication_batch, settings.stream_max_length
                            )
                            journal.heartbeat(
                                "publisher",
                                UUID(identity),
                                "ready",
                                {"last_batch": count, "scope": "control_and_audit_only"},
                            )
                        stop.wait(0.02 if count else 0.2)
                    except (SQLAlchemyError, RedisError, ValueError, RuntimeError) as error:
                        log.error(
                            "worker_cycle_blocked", extra={"reason": role, "error_type": type(error).__name__}
                        )
                        if role == "consumer":
                            bus.heartbeat("consumer", identity, "blocked", {"reason": type(error).__name__})
                        stop.wait(1)
    except Exception as error:
        log.error("worker_failed", extra={"reason": role, "error_type": type(error).__name__})
        raise SystemExit(1) from None
    finally:
        stop.set()
        if watchdog:
            watchdog.join(timeout=6)
        if lock:
            try:
                lock.release()
            except RedisError:
                pass
        bus.close()


if __name__ == "__main__":
    main()
