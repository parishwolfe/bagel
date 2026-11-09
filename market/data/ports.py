"""Small capabilities that other providers and storage implementations can implement."""

from collections.abc import AsyncIterator, Iterator, Sequence
from datetime import date
from typing import Protocol

from .domain import Bar, Instrument, MarketClock, Origin, TimeRange, TradingDay


class HistoricalBars(Protocol):
    def pages(self, symbols: Sequence[str], window: TimeRange) -> Iterator[list[Bar]]: ...


class InstrumentLookup(Protocol):
    def instrument(self, symbol: str) -> Instrument: ...


class TradingCalendar(Protocol):
    def calendar(self, start: date, end: date) -> dict[date, TradingDay]: ...

    def clock(self) -> MarketClock: ...


class StreamingBars(Protocol):
    def events(self, symbols: Sequence[str]) -> AsyncIterator[tuple[Bar, Origin]]: ...
