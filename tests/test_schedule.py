import asyncio
from datetime import UTC, date, datetime, timedelta

import pytest
from bagel_market_data.domain import NEW_YORK, MarketClock, TradingDay
from bagel_market_data.observability import Health, Metrics
from bagel_market_data.runtime import collect
from bagel_market_data.schedule import CollectionWindow, closure_delay, collection_window


class Provider:
    opened = False
    clock_calls = 0

    def calendar(self, *_):
        day = date(2026, 11, 27)  # Early close after Thanksgiving.
        return {
            day: TradingDay(
                day,
                datetime(2026, 11, 27, 9, 30, tzinfo=NEW_YORK),
                datetime(2026, 11, 27, 13, tzinfo=NEW_YORK),
            )
        }

    def clock(self):
        self.clock_calls += 1
        return MarketClock(datetime(2026, 11, 27, 15, tzinfo=UTC), self.opened)


def test_emergency_closure_does_not_disable_extended_hours(settings):
    provider = Provider()
    for hour in (4, 8, 13, 19):
        status = collection_window(
            settings, provider, datetime(2026, 11, 27, hour, tzinfo=NEW_YORK)
        )
        assert status.allowed and status.active
    assert provider.clock_calls == 0  # Regular-only clock is normally false in extended hours.
    now = datetime(2026, 11, 27, 10, tzinfo=NEW_YORK)
    assert collection_window(settings, provider, now).reason == "unexpected_regular_closure"
    provider.opened = True
    assert collection_window(settings, provider, now).allowed


def test_holiday_and_backoff_wake_before_extended_open(settings):
    now = datetime(2026, 11, 26, 18, tzinfo=NEW_YORK)
    status = collection_window(settings, Provider(), now)
    assert not status.allowed and status.reason == "non_trading_day"
    assert status.next_start == datetime(2026, 11, 27, 3, 59, tzinfo=NEW_YORK)
    assert [closure_delay(i, now, status.next_start) for i in range(5)] == [30, 60, 120, 240, 300]
    assert closure_delay(10, status.next_start - timedelta(seconds=5), status.next_start) == 5


@pytest.mark.asyncio
async def test_calendar_refresh_closes_quiet_stream(settings, monkeypatch):
    started, closed = asyncio.Event(), asyncio.Event()
    calls = 0

    def decision(*_):
        nonlocal calls
        calls += 1
        return CollectionWindow(None, calls == 1, calls == 1, "test", None)

    class Stream:
        async def events(self, _):
            started.set()
            try:
                await asyncio.Event().wait()
                yield  # pragma: no cover
            finally:
                closed.set()

    async def sleep(seconds):
        if seconds == 60:
            await started.wait()
        else:
            raise asyncio.CancelledError

    monkeypatch.setattr("bagel_market_data.runtime.collection_window", decision)
    monkeypatch.setattr("bagel_market_data.runtime.asyncio.sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await collect(settings, None, Stream(), asyncio.Queue(), Health(), Metrics())
    assert closed.is_set()
