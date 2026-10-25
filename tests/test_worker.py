import asyncio
import signal
from datetime import timedelta

import httpx
import pytest
from bagel_market_data.domain import Instrument
from bagel_market_data.runtime import run_worker

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_worker_serves_probes_and_stops_cleanly(
    store, settings, unused_tcp_port, monkeypatch
):
    class Provider:
        cancelled = False

        def instrument(self, symbol):
            return Instrument(symbol, symbol, symbol)

        def calendar(self, *_):
            return {}  # Closed market; a valid worker remains ready without a stream.

        def pages(self, *_):
            yield []

        def cancel(self):
            self.cancelled = True

    settings.health_port = unused_tcp_port
    settings.auto_backfill = True
    settings.history_start = (settings.eligible_end() - timedelta(days=2)).date().isoformat()
    callbacks = {}
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda sig, cb: callbacks.update({sig: cb}))
    provider = Provider()
    task = asyncio.create_task(run_worker(settings, store, provider))
    try:
        async with httpx.AsyncClient() as client:
            ready = False
            for _ in range(100):
                await asyncio.sleep(0.02)
                if task.done():
                    task.result()
                try:
                    response = await client.get(f"http://127.0.0.1:{unused_tcp_port}/health/ready")
                    metrics = await client.get(f"http://127.0.0.1:{unused_tcp_port}/metrics")
                    if response.status_code == 200 and "bagel_last_reconciliation" in metrics.text:
                        ready = True
                        break
                except httpx.ConnectError:
                    pass
            assert ready
            assert response.json()["connected"] is False
            assert metrics.status_code == 200
        callbacks[signal.SIGTERM]()
        await asyncio.wait_for(task, 5)
        assert provider.cancelled
        snapshot = store.watchlist("alpaca:iex")
        assert snapshot.loaded_revision == snapshot.revision and snapshot.status == "ready"
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_watchlist_change_requests_worker_restart(
    store, settings, unused_tcp_port, monkeypatch
):
    class Provider:
        def instrument(self, symbol):
            return Instrument(symbol, symbol, symbol)

        def calendar(self, *_):
            return {}

        def pages(self, *_):
            yield []

        def cancel(self):
            pass

    settings.health_port = unused_tcp_port
    settings.watchlist_poll_seconds = 1
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda *_: None)
    task = asyncio.create_task(run_worker(settings, store, Provider()))
    try:
        for _ in range(100):
            await asyncio.sleep(0.01)
            try:
                initial = store.watchlist("alpaca:iex")
                break
            except ValueError:
                pass
        changed = store.set_watchlist(settings.model_copy(update={"symbols": ["GLD"]}))
        assert changed.revision > initial.revision
        assert await asyncio.wait_for(task, 5) is True
        assert store.watchlist("alpaca:iex").status == "pending"
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
