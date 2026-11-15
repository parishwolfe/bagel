"""Pull historical bars from IB Gateway into the ParquetDataCatalog.

Two timeframes are downloaded directly from IB:

* ``1min`` — the 1-minute **base** series (default 2 years). Every intraday
  timeframe below daily is rebuilt from this with Nautilus composite bars, so we
  never download 5-/15-/60-minute data separately.
* ``1day`` — daily bars (default 15 years; IB has deep daily history).

IB specifics handled here
-------------------------
* The TWS-API historical calls are *blocking* but bridge to asyncio internally,
  so they must run inside a running event loop -> we wrap in ``asyncio.run`` and
  ``await`` whatever the call returns.
* Passing ``start_date_time`` lets Nautilus chunk the request internally and
  respect IB pacing; we don't page it ourselves.
* All symbols resolve on the ``ARCA`` venue with consolidated TRADES data.

Idempotency: a symbol+timeframe already in the catalog is skipped. Use
``force=True`` to re-pull (clears that series' existing range first).
"""

from __future__ import annotations

import asyncio
import datetime as dt

from nautilus_trader.adapters import interactive_brokers as ib
from nautilus_trader.model import InstrumentId

from bagel.config import settings
from bagel.ingest.catalog import open_catalog
from bagel.universe import SYMBOLS, instrument_ids

# timeframe -> (IB bar specification, default years of history)
TIMEFRAMES: dict[str, tuple[str, float]] = {
    "1min": ("1-MINUTE-LAST", 2.0),
    "1day": ("1-DAY-LAST", 15.0),
}


async def _await(value):
    """Await the result of an IB call if it returned an awaitable."""
    if asyncio.iscoroutine(value) or hasattr(value, "__await__"):
        return await value
    return value


def _bar_type_str(instrument_id: InstrumentId, bar_spec: str) -> str:
    return f"{instrument_id}-{bar_spec}-EXTERNAL"


def _existing_bar_count(catalog, bar_type_str: str) -> int:
    try:
        return len(catalog.query_bars(identifiers=[bar_type_str]))
    except Exception:
        return 0


async def _pull(
    ids: list[InstrumentId],
    bar_spec: str,
    years: float,
    client_id: int,
    force: bool,
) -> dict[str, int]:
    prov_cfg = ib.InteractiveBrokersInstrumentProviderConfig(
        symbology_method=ib.SymbologyMethod.SIMPLIFIED,
    )
    data_cfg = ib.InteractiveBrokersDataClientConfig(
        host=settings.ib_host,
        port=settings.ib_port,
        client_id=client_id,
        market_data_type=ib.MarketDataType.DELAYED,  # fine for historical TRADES
        connection_timeout=30,
        instrument_provider=prov_cfg,
    )
    provider = ib.InteractiveBrokersInstrumentProvider(prov_cfg)
    client = ib.HistoricalInteractiveBrokersClient(provider, data_cfg)

    catalog = open_catalog()
    end = dt.datetime.now(dt.UTC)  # per-symbol windows are computed in _request_bars

    results: dict[str, int] = {}
    for iid in ids:
        bar_type_str = _bar_type_str(iid, bar_spec)
        if not force and _existing_bar_count(catalog, bar_type_str) > 0:
            print(f"  {bar_type_str:38s} present -> skip (use --force)")
            results[bar_type_str] = 0
            continue
        try:
            results[bar_type_str] = await _pull_one(
                client, catalog, iid, bar_spec, bar_type_str, end, years, force
            )
        except Exception as e:  # one symbol failing must not abort the batch
            print(f"  {bar_type_str:38s} ERROR: {type(e).__name__}: {str(e)[:70]}")
            results[bar_type_str] = 0
    return results


async def _request_bars(client, iid, bar_spec, end, years):
    """Request bars, falling back to shorter windows when IB has no deep history.

    IB raises error 162 ("HMDS query returned no data") when the window starts
    before the data the exchange holds for that symbol. We retry with
    progressively more recent start dates so a shallow-history symbol still
    yields whatever it does have.
    """
    windows = sorted({years, 10.0, 5.0, 2.0, 1.0}, reverse=True)
    for yrs in windows:
        if yrs > years:
            continue
        start = end - dt.timedelta(days=round(yrs * 365.25))
        try:
            bars = await _await(
                client.request_bars(
                    bar_specifications=[bar_spec],
                    end_date_time=end,
                    start_date_time=start,
                    instrument_ids=[iid],
                    use_rth=settings.use_rth,
                    timeout=600,
                )
            )
            if bars:
                return bars
        except Exception as e:
            if "no data" not in str(e).lower():
                raise
    return []


async def _pull_one(
    client, catalog, iid, bar_spec, bar_type_str, end, years, force
) -> int:
    # Instrument definition first (needed by the backtest engine).
    insts = await _await(client.request_instruments(instrument_ids=[iid]))
    if insts:
        catalog.write_instruments(insts)

    bars = await _request_bars(client, iid, bar_spec, end, years)
    if bars:
        if force:
            try:
                catalog.delete_data_range(type(bars[0]), bar_type_str)
            except Exception:
                pass
        catalog.write_bars(bars)
        first = dt.datetime.fromtimestamp(bars[0].ts_event / 1e9, dt.UTC)
        last = dt.datetime.fromtimestamp(bars[-1].ts_event / 1e9, dt.UTC)
        print(
            f"  {bar_type_str:38s} {len(bars):>8,} bars  {first:%Y-%m-%d} -> {last:%Y-%m-%d}"
        )
    else:
        print(f"  {bar_type_str:38s} {0:>8,} bars  (no data returned)")
    return len(bars)


def download(
    symbols: list[str] | None = None,
    timeframe: str = "1min",
    years: float | None = None,
    force: bool = False,
    client_id: int | None = None,
) -> dict[str, int]:
    """Download ``years`` of ``timeframe`` bars for ``symbols`` into the catalog."""
    if timeframe not in TIMEFRAMES:
        raise ValueError(f"timeframe must be one of {list(TIMEFRAMES)}")
    bar_spec, default_years = TIMEFRAMES[timeframe]
    yrs = default_years if years is None else years
    ids = instrument_ids(symbols)
    cid = client_id if client_id is not None else settings.ib_client_id + 50
    print(
        f"Pulling {yrs:g}y of {timeframe} bars for {len(ids)} symbols "
        f"from IB {settings.ib_host}:{settings.ib_port} (RTH={settings.use_rth})"
    )
    results = asyncio.run(_pull(ids, bar_spec, yrs, cid, force))
    total = sum(results.values())
    print(f"Done. {total:,} {timeframe} bars written to {settings.catalog_path}")
    return results


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Pull IB bars -> catalog")
    parser.add_argument(
        "--symbols",
        nargs="*",
        default=None,
        help=f"subset of {', '.join(SYMBOLS)} (default: all)",
    )
    parser.add_argument(
        "--timeframe",
        choices=[*TIMEFRAMES, "all"],
        default="1min",
        help="1min (default), 1day, or all",
    )
    parser.add_argument(
        "--years", type=float, default=None, help="override default per timeframe"
    )
    parser.add_argument("--force", action="store_true", help="re-pull existing series")
    parser.add_argument("--client-id", type=int, default=None)
    args = parser.parse_args()

    timeframes = list(TIMEFRAMES) if args.timeframe == "all" else [args.timeframe]
    for tf in timeframes:
        download(args.symbols, tf, args.years, args.force, args.client_id)


if __name__ == "__main__":
    main()
