"""Provider-neutral lifecycle and capabilities. SDK payloads stay in adapters."""

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date, time
from typing import Protocol

from market.data.domain import Bar, Instrument, TimeRange, TradingDay

MASSIVE_STOCK_FEED = "us-stock-qualified-trades-raw"


@dataclass(frozen=True)
class Capabilities:
    provider: str
    streaming: bool
    historical: bool
    corrections: bool
    replay: bool
    original_publication_history: bool


class Collector(Protocol):
    source: str
    capabilities: Capabilities

    def pages(self, symbols: Sequence[str], window: TimeRange) -> Iterator[list[Bar]]: ...
    def instrument(self, symbol: str) -> Instrument: ...
    def calendar(self, start: date, end: date) -> dict[date, TradingDay]: ...
    def close(self) -> None: ...
    def cancel(self) -> None: ...


class HistoryConfiguration(Protocol):
    symbols: list[str]

    @property
    def feed(self) -> str: ...

    window_open: time
    window_close: time
    history_symbol_batch: int
    batch_size: int
    database_budget_mb: int
