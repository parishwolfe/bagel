"""Bounded offline reader for documented Massive US-stock minute flat files.

Downloading from S3 and obtaining entitlements remain operator/provider concerns.
An explicit stable-identity catalogue prevents ticker text from merging securities.
"""

import csv
import gzip
import hashlib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from market.data.domain import Bar, Instrument, TimeRange

COLUMNS = {"ticker", "volume", "open", "close", "high", "low", "window_start", "transactions"}


class FileInstrument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    provider_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=200)


class FileCatalogue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    format: Literal["massive-us-stock-minute-v1"]
    instruments: dict[str, FileInstrument] = Field(min_length=1)

    def selected(self) -> list[Instrument]:
        import re

        if any(not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,19}", symbol) for symbol in self.instruments):
            raise ValueError("Catalogue contains an invalid stock symbol")
        return [
            Instrument(entry.provider_id, symbol, entry.name) for symbol, entry in self.instruments.items()
        ]


def verify_file(path: Path, expected_sha256: str) -> None:
    import re

    if not re.fullmatch(r"[a-f0-9]{64}", expected_sha256):
        raise ValueError("A SHA-256 checksum of the source file is required")
    with path.open("rb") as source:
        actual = hashlib.file_digest(source, "sha256").hexdigest()
    if actual != expected_sha256:
        raise ValueError("Flat-file checksum mismatch")


def read_minutes(
    path: Path, catalogue: FileCatalogue, window: TimeRange, row_budget: int = 100_000_000
) -> Iterator[Bar]:
    if row_budget < 1:
        raise ValueError("Flat-file row budget must be positive")
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8", newline="") as source:
        rows = csv.DictReader(source)
        if set(rows.fieldnames or ()) != COLUMNS or len(rows.fieldnames or ()) != len(COLUMNS):
            raise ValueError("File is not the documented stock minute aggregate schema")
        for ordinal, row in enumerate(rows, 1):
            if ordinal > row_budget:
                raise ValueError("Flat file exceeds its explicit row resource budget")
            if None in row or any(value is None for value in row.values()):
                raise ValueError("Malformed flat-file record")
            symbol = row["ticker"]
            if symbol not in catalogue.instruments:
                continue
            nanoseconds = int(row["window_start"])
            if nanoseconds % 60_000_000_000:
                raise ValueError("Stock minute timestamp is not aligned to a completed minute")
            stamp = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(seconds=nanoseconds // 1_000_000_000)
            if not window.start <= stamp < window.end:
                continue
            yield Bar(
                symbol,
                stamp,
                Decimal(row["open"]),
                Decimal(row["high"]),
                Decimal(row["low"]),
                Decimal(row["close"]),
                Decimal(row["volume"]),
                None,
                int(row["transactions"]),
            )
