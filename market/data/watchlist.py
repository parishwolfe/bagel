"""Persistent watchlist versions and explicit historical-load failure modes."""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime

from .config import Settings


@dataclass(frozen=True)
class Watchlist:
    stream_key: str
    symbols: tuple[str, ...]
    revision: int
    loaded_revision: int | None
    attempt_years: int | None
    history_start: datetime | None
    history_end: datetime | None
    status: str


def configuration_signature(settings: Settings) -> str:
    content = {
        "symbols": sorted(settings.symbols),
        "years": settings.max_history_years,
        "start": settings.history_start,
        "end": settings.history_end,
    }
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


class WatchlistChanged(RuntimeError):
    pass


class CapacityExceeded(RuntimeError):
    pass


class HistoryDoesNotFit(RuntimeError):
    pass
