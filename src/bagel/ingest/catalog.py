"""Thin helpers around the Nautilus ParquetDataCatalog.

The catalog is the single source of truth for offline backtests: the ingest
step is the only writer, and strategies/backtests only ever read from it.
"""

from __future__ import annotations

from nautilus_trader.persistence import ParquetDataCatalog

from bagel.config import settings


def open_catalog() -> ParquetDataCatalog:
    """Open (creating directories if needed) the project ParquetDataCatalog."""
    settings.ensure_dirs()
    return ParquetDataCatalog(str(settings.catalog_path))
