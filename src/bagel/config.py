"""Central configuration for the bagel trading lab.

Values come from environment variables (optionally loaded from a local ``.env``
file at the project root), with sensible defaults that match a locally running
**IB Gateway paper** session. Nothing here is secret; real credentials live only
in ``.env`` (gitignored) and are read by IB Gateway itself, not by this code.

Recognised environment variables
--------------------------------
IB_HOST          IB Gateway host            (default: 127.0.0.1)
IB_PORT          IB Gateway API port        (default: 4002  -> Gateway paper)
IB_CLIENT_ID     base TWS-API client id     (default: 1)
CATALOG_PATH     ParquetDataCatalog root    (default: <project>/data/catalog)
BAGEL_CAPITAL    backtest starting cash USD (default: 50_000)
USE_RTH          regular-hours-only bars    (default: true)
FIRSTRATE_SOURCE FirstRate data zips (read)  (default: /Volumes/Seagate/raid5/FirstRateData)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _project_root() -> Path:
    """Walk up from this file until we find the pyproject.toml (repo root)."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists():
            return parent
    return here.parents[2]


ROOT = _project_root()


def _load_dotenv(path: Path) -> None:
    """Minimal ``.env`` loader (no third-party dependency).

    Only sets variables that are not already present in the environment, so an
    explicitly exported variable always wins.
    """
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv(ROOT / ".env")


def _as_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # --- IB Gateway connection (paper) ---
    ib_host: str = os.environ.get("IB_HOST", "127.0.0.1")
    ib_port: int = int(os.environ.get("IB_PORT", "4002"))
    ib_client_id: int = int(os.environ.get("IB_CLIENT_ID", "1"))

    # --- data ---
    catalog_path: Path = Path(
        os.environ.get("CATALOG_PATH", str(ROOT / "data" / "catalog"))
    )
    raw_path: Path = ROOT / "data" / "raw"
    use_rth: bool = _as_bool(os.environ.get("USE_RTH", "true"))

    # --- FirstRate (vendor) data: read-only source of the ES futures zips ---
    firstrate_source: Path = Path(
        os.environ.get("FIRSTRATE_SOURCE", "/Volumes/Seagate/raid5/FirstRateData")
    )

    # --- backtest defaults ---
    venue: str = "ARCA"  # all our ETFs resolve on NYSE Arca via IB
    currency: str = "USD"
    starting_capital: int = int(os.environ.get("BAGEL_CAPITAL", "50000"))

    def ensure_dirs(self) -> Settings:
        self.catalog_path.mkdir(parents=True, exist_ok=True)
        self.raw_path.mkdir(parents=True, exist_ok=True)
        return self


settings = Settings()
