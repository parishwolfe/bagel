import asyncio
from contextlib import contextmanager
from datetime import UTC, date, datetime
from types import SimpleNamespace

import psycopg
import pytest
from bagel_market_data.domain import NEW_YORK, MarketClock, Observation, Origin, TradingDay
from bagel_market_data.observability import Health, Metrics
from bagel_market_data.providers.alpaca import TransientProviderError
from bagel_market_data.runtime import collect, heartbeat, repair, stream_ownership, write_batches
from bagel_market_data.storage import StreamAlreadyOwned
from conftest import make_bar


@pytest.mark.asyncio
async def test_failed_batch_is_retried_and_shutdown_drains(settings, monkeypatch):
    class Store:
        attempts = 0
        written = []

        def write_live(self, series, batch):
            self.attempts += 1
            if self.attempts == 1:
                raise psycopg.OperationalError("test database outage")
            self.written.extend(batch)

    async def no_sleep(_):
        pass

    monkeypatch.setattr("bagel_market_data.runtime.asyncio.sleep", no_sleep)
    store, queue, health, metrics = Store(), asyncio.Queue(maxsize=100), Health(), Metrics()
    item = Observation(make_bar(), "regular", Origin.LIVE, datetime.now(UTC), datetime.now(UTC))
    await queue.put(item)
    await queue.put(None)
    await write_batches(settings, store, {"SPY": 1}, queue, health, metrics)
    await queue.join()
    assert store.attempts == 2 and store.written == [item]
    assert health.database_ok


@pytest.mark.asyncio
async def test_lost_lock_session_is_fatal():
    class Connection:
        def execute(self, _):
            raise psycopg.OperationalError("lost lock session")

    with pytest.raises(psycopg.OperationalError):
        await heartbeat(Connection(), Health())


@pytest.mark.asyncio
async def test_collection_reconnects_and_retains_valid_bars(settings, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 22, 14, 1, tzinfo=UTC)

    class Provider:
        def clock(self):
            return MarketClock(Clock.now(UTC), True)

        def calendar(self, *_):
            return {
                date(2026, 10, 22): TradingDay(
                    date(2026, 10, 22),
                    datetime(2026, 10, 22, 9, 30, tzinfo=NEW_YORK),
                    datetime(2026, 10, 22, 16, tzinfo=NEW_YORK),
                )
            }

    class Stream:
        async def events(self, symbols):
            yield make_bar(), Origin.UPDATED
            raise TransientProviderError("test disconnect")

    async def stop_on_backoff(_):
        raise asyncio.CancelledError

    monkeypatch.setattr("bagel_market_data.runtime.datetime", Clock)
    monkeypatch.setattr("bagel_market_data.runtime.asyncio.sleep", stop_on_backoff)
    queue, health, metrics = asyncio.Queue(), Health(), Metrics()
    with pytest.raises(asyncio.CancelledError):
        await collect(settings, Provider(), Stream(), queue, health, metrics)
    assert (await queue.get()).origin == Origin.UPDATED
    assert health.session_active


@pytest.mark.asyncio
async def test_holiday_never_opens_stream(settings, monkeypatch):
    class Provider:
        def calendar(self, *_):
            return {}

    class Stream:
        async def events(self, _):
            pytest.fail("A holiday must not open a stream")
            yield

    async def stop(_):
        raise asyncio.CancelledError

    monkeypatch.setattr("bagel_market_data.runtime.asyncio.sleep", stop)
    health = Health()
    with pytest.raises(asyncio.CancelledError):
        await collect(settings, Provider(), Stream(), asyncio.Queue(), health, Metrics())
    assert not health.session_active


def test_closed_market_is_ready_without_stream():
    health = Health(database_ok=True)
    assert health.ready
    health.session_active = True
    assert not health.ready
    health.connected = True
    assert health.ready
    health.stopping = True
    assert not health.live


@pytest.mark.asyncio
async def test_first_correction_sweep_runs_on_new_container_host(settings, monkeypatch):
    calls = []

    class Store:
        def repair_cursor(self, *_):
            return settings.eligible_end()

        def pending_ranges(self):
            return 0

    async def stop(_):
        raise asyncio.CancelledError

    monkeypatch.setattr("bagel_market_data.runtime.time", SimpleNamespace(monotonic=lambda: 0))
    monkeypatch.setattr(
        "bagel_market_data.runtime.ingest_history", lambda *a, **kw: calls.append(kw)
    )
    monkeypatch.setattr("bagel_market_data.runtime.asyncio.sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        await repair(settings, None, Store(), {"SPY": 1}, "test", Health(), Metrics())
    assert len(calls) == 1 and calls[0]["force"]


@pytest.mark.asyncio
async def test_rolling_revision_waits_for_old_owner_and_stays_live(monkeypatch):
    class Store:
        attempts = 0
        released = False

        @contextmanager
        def singleton(self, _):
            self.attempts += 1
            if self.attempts < 3:
                raise StreamAlreadyOwned("old revision draining")
            try:
                yield "connection"
            finally:
                self.released = True

    health, store = Health(), Store()

    async def wait(_):
        assert health.live and not health.ready

    monkeypatch.setattr("bagel_market_data.runtime.asyncio.sleep", wait)
    async with stream_ownership(store, "test", health, asyncio.Event()) as connection:
        assert connection == "connection" and store.attempts == 3
    assert store.released
