"""Ingest FirstRate ES futures data into the ParquetDataCatalog — ephemerally.

FirstRate ships the E-mini S&P 500 (ES) in a few shapes; see the ``data model``
notes below. For a continuous backtest the right input is the **continuous
back-adjusted** series, which FirstRate already delivers as a single, fully
roll-stitched file per timeframe — so there is nothing to *combine* for that
series (the per-contract ``archive`` + ``update`` pairs would only need
combining if you were stitching individual expiries yourself, which the
continuous series supersedes).

Ephemeral ingestion
-------------------
The source zips on the external drive are treated as **read-only** and are never
modified. Each run extracts the needed member into a temporary directory, parses
it straight into Nautilus ``Bar`` objects written to the catalog in chunks, and
then deletes the temporary directory. No raw CSVs are left behind in the project
(``data/raw/`` stays empty); the catalog is the only durable artifact.

Futures specifics are handled by the instrument definition in
:mod:`bagel.futures` (CME venue, ``$50``/point multiplier, ``0.25`` tick), not
here — this module only turns rows into bars.

Source layout (``FIRSTRATE_SOURCE``)
------------------------------------
* ``ES_full_1day_continuous_adjusted*.zip`` -> ``...,o,h,l,c,volume,open_interest``
* ``ES_full_1min_continuous_adjusted*.zip`` -> ``...,o,h,l,c,volume``
"""

from __future__ import annotations

import datetime as dt
import tempfile
import zipfile
from pathlib import Path

from nautilus_trader.model import Bar, BarType, Price, Quantity

from bagel.config import settings
from bagel.futures import ES_PRICE_PRECISION, es_bar_type, es_instrument
from bagel.ingest.catalog import open_catalog

# timeframe -> glob for the continuous-adjusted source zip
_SOURCE_GLOB = {
    "1day": "ES_full_1day_continuous_adjusted*.zip",
    "1min": "ES_full_1min_continuous_adjusted*.zip",
}
_CHUNK = 500_000  # bars per catalog write (bounds memory on the 5.5M-row 1-min file)


def _find_zip(timeframe: str) -> Path:
    source = settings.firstrate_source
    if not source.exists():
        raise FileNotFoundError(
            f"FirstRate source not found: {source} "
            f"(set FIRSTRATE_SOURCE or mount the drive)."
        )
    matches = sorted(source.glob(_SOURCE_GLOB[timeframe]))
    if not matches:
        raise FileNotFoundError(
            f"No {timeframe} continuous-adjusted zip in {source} "
            f"(looked for {_SOURCE_GLOB[timeframe]})."
        )
    return matches[0]


def _parse_bar(bar_type: BarType, line: str) -> Bar | None:
    """Parse one FirstRate CSV row into a Bar, or None if unusable.

    Timestamps are the vendor's wall-clock (US/Eastern); we store them as-is
    interpreted on a fixed clock — a constant offset that does not affect a
    single-instrument backtest. Daily rows carry a trailing open-interest column
    we ignore (bars store volume only).
    """
    parts = line.split(",")
    if len(parts) < 6:
        return None
    stamp, o, h, low, c, vol = (
        parts[0],
        parts[1],
        parts[2],
        parts[3],
        parts[4],
        parts[5],
    )
    try:
        ts = int(dt.datetime.fromisoformat(stamp).replace(tzinfo=dt.UTC).timestamp())
        ts_ns = ts * 1_000_000_000
        p = ES_PRICE_PRECISION
        open_, high, lo, close = (
            Price(float(o), p),
            Price(float(h), p),
            Price(float(low), p),
            Price(float(c), p),
        )
        volume = Quantity.from_int(max(int(float(vol)), 0))
    except ValueError, IndexError:
        return None
    return Bar(bar_type, open_, high, lo, close, volume, ts_ns, ts_ns)


def ingest(timeframe: str, force: bool = False) -> int:
    """Ingest the continuous ES ``timeframe`` series into the catalog.

    Returns the number of bars written. A series already present is skipped
    unless ``force`` is set.
    """
    if timeframe not in _SOURCE_GLOB:
        raise ValueError(f"timeframe must be one of {list(_SOURCE_GLOB)}")

    catalog = open_catalog()
    bar_type = es_bar_type(timeframe)
    bar_type_str = str(bar_type)

    if not force:
        try:
            existing = len(catalog.query_bars(identifiers=[bar_type_str]))
        except Exception:
            existing = 0
        if existing:
            print(
                f"  {bar_type_str:34s} present ({existing:,} bars) -> skip (use --force)"
            )
            return 0

    zip_path = _find_zip(timeframe)
    print(f"  {bar_type_str:34s} reading {zip_path.name}")

    # Instrument definition first (the engine needs it to size/mark futures PnL).
    catalog.write_instruments([es_instrument()])

    total = 0
    first_stamp = last_stamp = ""
    with tempfile.TemporaryDirectory(prefix="bagel-firstrate-") as tmp:
        with zipfile.ZipFile(zip_path) as zf:
            member = next(n for n in zf.namelist() if n.lower().endswith(".txt"))
            zf.extract(member, tmp)  # extraction is ephemeral: tmp is deleted below
        extracted = Path(tmp) / member

        chunk: list[Bar] = []
        with extracted.open("r") as fh:
            for line in fh:
                line = line.rstrip("\n")
                if not line:
                    continue
                bar = _parse_bar(bar_type, line)
                if bar is None:
                    continue
                if not first_stamp:
                    first_stamp = line.split(",", 1)[0]
                last_stamp = line.split(",", 1)[0]
                chunk.append(bar)
                if len(chunk) >= _CHUNK:
                    catalog.write_bars(chunk)
                    total += len(chunk)
                    chunk = []
        if chunk:
            catalog.write_bars(chunk)
            total += len(chunk)

    span = f"  {first_stamp} -> {last_stamp}" if first_stamp else ""
    print(f"  {bar_type_str:34s} {total:>9,} bars{span}")
    return total


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Ingest FirstRate ES futures (continuous back-adjusted) -> catalog"
    )
    parser.add_argument(
        "--timeframe",
        choices=["1day", "1min", "all"],
        default="1day",
        help="1day (default, fast), 1min (~5.5M bars), or all",
    )
    parser.add_argument(
        "--force", action="store_true", help="re-ingest if already present"
    )
    args = parser.parse_args()

    timeframes = ["1day", "1min"] if args.timeframe == "all" else [args.timeframe]
    print(f"Ingesting FirstRate ES from {settings.firstrate_source} (read-only)")
    grand = 0
    for tf in timeframes:
        grand += ingest(tf, force=args.force)
    print(f"Done. {grand:,} bars written to {settings.catalog_path}")


if __name__ == "__main__":
    main()
