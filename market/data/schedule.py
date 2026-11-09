"""Calendar and clock gates. Extended hours must not depend on regular clock.is_open."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .config import Settings
from .domain import NEW_YORK, TradingDay
from .providers.alpaca import TransientProviderError


@dataclass(frozen=True)
class CollectionWindow:
    day: TradingDay | None
    allowed: bool
    active: bool
    reason: str
    next_start: datetime | None


def collection_window(settings: Settings, provider, now: datetime) -> CollectionWindow:
    local_day = now.astimezone(NEW_YORK).date()
    calendar = provider.calendar(local_day, local_day + timedelta(days=8))
    upcoming = [
        datetime.combine(day, settings.window_open, NEW_YORK).astimezone(UTC)
        - timedelta(seconds=settings.connection_grace_seconds)
        for day in sorted(calendar)
    ]
    next_start = next((value for value in upcoming if value > now), None)
    day = calendar.get(local_day)
    if day is None:
        return CollectionWindow(None, False, False, "non_trading_day", next_start)
    opening = datetime.combine(local_day, settings.window_open, NEW_YORK).astimezone(UTC)
    closing = datetime.combine(local_day, settings.window_close, NEW_YORK).astimezone(UTC)
    begin = opening - timedelta(seconds=settings.connection_grace_seconds)
    finish = closing + timedelta(seconds=settings.correction_grace_seconds)
    if not begin <= now < finish:
        return CollectionWindow(day, False, False, "outside_extended_session", next_start)
    # A false clock during extended hours is normal. Only use it to detect an
    # unexpected exchange closure inside the calendar's regular session.
    if day.open <= now < day.close:
        clock = provider.clock()
        if abs((now - clock.timestamp).total_seconds()) > 180:
            raise TransientProviderError("Stale market clock")
        if not clock.is_open:
            return CollectionWindow(day, False, False, "unexpected_regular_closure", next_start)
    return CollectionWindow(day, True, opening <= now < closing, "collecting", next_start)


def closure_delay(attempt: int, now: datetime, next_start: datetime | None) -> float:
    delay = min(300.0, 30.0 * 2 ** min(attempt, 4))
    if next_start is not None:
        delay = min(delay, max(1.0, (next_start - now).total_seconds()))
    return delay
