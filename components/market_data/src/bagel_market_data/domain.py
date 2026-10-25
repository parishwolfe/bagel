"""Normalized domain types. Provider SDK types must not cross this boundary."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")
MINUTE = timedelta(minutes=1)


def utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Timestamps must include a timezone")
    return value.astimezone(UTC)


def subtract_years(value: datetime, years: int) -> datetime:
    try:
        return value.replace(year=value.year - years)
    except ValueError:  # February 29 -> February 28
        return value.replace(year=value.year - years, day=28)


def parse_boundary(value: str) -> datetime:
    """Dates mean NY midnight; timestamps require an explicit UTC offset."""
    if len(value) == 10:
        return datetime.combine(date.fromisoformat(value), time.min, NEW_YORK).astimezone(UTC)
    return utc(datetime.fromisoformat(value))


@dataclass(frozen=True)
class TimeRange:
    """Half-open interval [start, end)."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "start", utc(self.start))
        object.__setattr__(self, "end", utc(self.end))
        if self.start >= self.end:
            raise ValueError("start must precede end")
        if any(t.second or t.microsecond for t in (self.start, self.end)):
            raise ValueError("Date boundaries must align to whole minutes")

    def chunks(self):
        """UTC day boundaries make checkpoints reproducible across invocations."""
        start = self.start
        while start < self.end:
            end = min((start + timedelta(days=1)).replace(hour=0, minute=0), self.end)
            yield TimeRange(start, end)
            start = end


class Origin(StrEnum):
    LIVE = "live"
    UPDATED = "live_updated"
    HISTORICAL = "historical"


@dataclass(frozen=True)
class Instrument:
    provider_id: str
    symbol: str
    name: str
    asset_class: str = "us_equity"
    currency: str = "USD"


@dataclass(frozen=True)
class Bar:
    symbol: str
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    vwap: Decimal | None
    trade_count: int | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", utc(self.timestamp))
        if self.timestamp.second or self.timestamp.microsecond:
            raise ValueError("Bar timestamp must be a minute boundary")
        values = (self.open, self.high, self.low, self.close, self.volume)
        if not all(v.is_finite() for v in values):
            raise ValueError("Bar values must be finite")
        if min(self.open, self.high, self.low, self.close) <= 0 or self.volume < 0:
            raise ValueError("Bar prices must be positive and volume nonnegative")
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise ValueError("OHLC values are inconsistent")
        if self.vwap is not None and (not self.vwap.is_finite() or self.vwap <= 0):
            raise ValueError("VWAP must be finite and positive")
        if self.trade_count is not None and self.trade_count < 0:
            raise ValueError("Trade count must be nonnegative")


@dataclass(frozen=True)
class MarketClock:
    timestamp: datetime
    is_open: bool


@dataclass(frozen=True)
class TradingDay:
    day: date
    open: datetime
    close: datetime

    def classify(self, timestamp: datetime, window_open: time, window_close: time) -> str | None:
        local = utc(timestamp).astimezone(NEW_YORK)
        if local.date() != self.day or not window_open <= local.time() < window_close:
            return None
        if timestamp < self.open:
            return "pre"
        if timestamp < self.close:
            return "regular"
        return "post"


@dataclass(frozen=True)
class Observation:
    bar: Bar
    session: str
    origin: Origin
    observed_at: datetime
    received_at: datetime
