"""One bounded subprocess per native Nautilus run. JSON is IPC, never strategy config."""

import ast
import importlib.util
import json
import math
import resource
import sys
from datetime import UTC, datetime, timedelta
from itertools import islice


def load(path):
    # A small mistake filter, NOT a Python sandbox. Run under a restricted OS user.
    tree = ast.parse(path.read_text())
    allowed = {
        "__future__",
        "datetime",
        "zoneinfo",
        "itertools",
        "math",
        "statistics",
        "collections",
        "decimal",
        "dataclasses",
        "typing",
        "nautilus_trader.model",
        "nautilus_trader.indicators",
        "nautilus_trader.trading",
    }
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module]
            )
            if any(n not in allowed for n in names) or getattr(node, "level", 0):
                raise ValueError(f"Unsupported strategy import on line {node.lineno}")
        if isinstance(node, ast.Name) and node.id in {
            "open",
            "exec",
            "eval",
            "compile",
            "__import__",
            "breakpoint",
            "input",
        }:
            raise ValueError(f"Unsupported strategy operation: {node.id}")
    spec = importlib.util.spec_from_file_location("lox_strategy", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def ns(date):
    return int(datetime.fromisoformat(date).replace(tzinfo=UTC).timestamp() * 1e9)


def describe(module, config):
    from nautilus_trader.model import BarType, InstrumentId
    from nautilus_trader.trading import Strategy

    if not issubclass(module.STRATEGY, Strategy):
        raise ValueError("STRATEGY must be a native Nautilus Strategy subclass")
    if module.TIMEFRAME not in config["windows"]:
        raise ValueError("TIMEFRAME must be 1min or 1day")
    if not module.SYMBOLS or any(
        s not in ("SPY", "QQQ", "IWM", "DIA", "TLT", "GLD", "UPRO", "TQQQ")
        for s in module.SYMBOLS
    ):
        raise ValueError("Start with the existing ETF universe")
    grid = list(islice(module.parameters(), config["max_trials"] + 1))
    if not 50 <= len(grid) or len(grid) * len(module.SYMBOLS) > config["max_trials"]:
        raise ValueError(
            "Need >=50 configurations, within max_trials including symbols"
        )
    if any(
        not isinstance(p, dict) or not isinstance(p.get("variation"), str) for p in grid
    ):
        raise ValueError("Each parameter dict needs a variation name")
    if len({json.dumps(p, sort_keys=True, allow_nan=False) for p in grid}) != len(grid):
        raise ValueError("Duplicate parameter configurations")
    if len({p["variation"] for p in grid}) < 3:
        raise ValueError("Need at least three meaningful variations")
    iid = InstrumentId.from_str(f"{module.SYMBOLS[0]}.ARCA")
    spec = "1-MINUTE" if module.TIMEFRAME == "1min" else "1-DAY"
    for params in grid:
        module.STRATEGY(
            instrument_id=iid,
            bar_type=BarType.from_str(f"{iid}-{spec}-LAST-EXTERNAL"),
            **params,
        )
    return dict(timeframe=module.TIMEFRAME, symbols=list(module.SYMBOLS), grid=grid)


def annual(total, years):
    return -1.0 if total <= -1 else math.expm1(math.log1p(total) / years)


def money(value):
    return float(str(value).split()[0].replace(",", ""))


def measure(engine, bars, capital):
    """Cash flows plus daily marked holdings, including unrealized P&L and fees."""
    import pandas as pd

    closes = (
        pd.Series(
            [float(b.close) for b in bars],
            index=pd.to_datetime([b.ts_event for b in bars], utc=True)
            .tz_convert("America/New_York")
            .date,
        )
        .groupby(level=0)
        .last()
    )
    fills = engine.generate_fills_report()
    equity = pd.Series(float(capital), index=closes.index)
    gross_max = 0.0
    if len(fills):
        fills = fills.sort_values("ts_event", kind="stable")
        signed = (
            fills["last_qty"]
            .astype(float)
            .where(
                fills["order_side"].astype(str).str.contains("BUY"),
                -fills["last_qty"].astype(float),
            )
        )
        flow = -signed * fills["last_px"].astype(float) - fills["commission"].map(money)
        days = (
            pd.to_datetime(fills["ts_event"], utc=True)
            .dt.tz_convert("America/New_York")
            .dt.date
        )
        cash = (
            capital
            + flow.groupby(days).sum().reindex(closes.index, fill_value=0).cumsum()
        )
        held = signed.groupby(days).sum().reindex(closes.index, fill_value=0).cumsum()
        equity = cash + held * closes
        fill_cash = capital + flow.cumsum()
        fill_exposure = signed.cumsum() * fills["last_px"].astype(float)
        fill_equity = fill_cash + fill_exposure
        if (fill_equity <= 0).any():
            raise ValueError("Account equity exhausted")
        gross_max = float((fill_exposure.abs() / fill_equity).max())
    if gross_max > 1.02:
        raise ValueError(f"Gross exposure exceeded 1x: {gross_max:.3f}")
    positions = engine.generate_positions_report()
    trades = int(positions["ts_closed"].notna().sum()) if len(positions) else 0
    years = ((closes.index[-1] - closes.index[0]).days + 1) / 365.25
    total = float(equity.iloc[-1] / capital - 1)
    peak = equity.cummax().clip(lower=capital)
    benchmark = float(closes.iloc[-1] / float(bars[0].open) - 1)
    return dict(
        bars=len(bars),
        trades=trades,
        return_pct=100 * total,
        annual_return=annual(total, years),
        max_drawdown=float((equity / peak - 1).min()),
        benchmark_annual_return=annual(benchmark, years),
        final_equity=float(equity.iloc[-1]),
        max_exposure=gross_max,
    )


def backtest(module, job):
    from nautilus_trader.backtest import BacktestEngine, BacktestEngineConfig
    from nautilus_trader.common import LogLevel
    from nautilus_trader.config import DataEngineConfig, LoggerConfig
    from nautilus_trader.execution import (
        FixedFeeModel,
        OneTickSlippageFillModel,
        StaticLatencyModel,
    )
    from nautilus_trader.model import AccountType, BarType, Currency, Money, OmsType
    from nautilus_trader.persistence import ParquetDataCatalog

    config = job["config"]
    window = config["windows"][module.TIMEFRAME]
    lo, mid, hi = (
        ns(window[k]) for k in ("train_start", "train_end", "validation_end")
    )
    if not lo < mid < hi:
        raise ValueError("Windows must be ordered, disjoint and nonempty")
    start, end = (lo, mid) if job["window"] != "validation" else (mid, hi)
    if job["window"] == "smoke":
        end = min(end, start + int(timedelta(days=31).total_seconds() * 1e9))
    catalog = ParquetDataCatalog(config["catalog"])
    iid = f"{job['symbol']}.ARCA"
    instruments = catalog.instruments(instrument_ids=[iid])
    if not instruments:
        raise ValueError(f"Missing instrument: {iid}; add a data request")
    instrument = instruments[0]
    spec = "1-MINUTE" if module.TIMEFRAME == "1min" else "1-DAY"
    bar_type = BarType.from_str(f"{iid}-{spec}-LAST-EXTERNAL")
    # Query only the selected window. Never load the vault then slice it.
    bars = catalog.query_bars(identifiers=[str(bar_type)], start=start, end=end - 1)
    if len(bars) < 2:
        raise ValueError(f"Missing {job['window']} data for {iid}; add a data request")
    week = 7 * 86400 * 10**9
    if bars[0].ts_event - start > week or end - bars[-1].ts_event > week:
        raise ValueError(
            f"Incomplete {job['window']} coverage for {iid}; add a data request"
        )
    stamps = [b.ts_event for b in bars]
    if any(a >= b for a, b in zip(stamps, stamps[1:])):
        raise ValueError("Duplicate or unsorted bars")
    errors = []

    def checked(method):
        def callback(self, *args):
            try:
                return method(self, *args)
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}")
                self.stop()

        return callback

    # rc9 logs/swallow callbacks' Python exceptions. Make them failed runs.
    overrides = {
        name: checked(getattr(module.STRATEGY, name))
        for name in dir(module.STRATEGY)
        if name.startswith("on_") and callable(getattr(module.STRATEGY, name))
    }

    def rejected(self, event):
        errors.append(f"{type(event).__name__}: {event.reason}")
        self.stop()

    overrides.update(on_order_rejected=rejected, on_order_denied=rejected)
    cls = type("CheckedStrategy", (module.STRATEGY,), overrides)
    strategy = cls(instrument_id=instrument.id, bar_type=bar_type, **job["params"])
    engine = BacktestEngine(
        config=BacktestEngineConfig(
            logging=LoggerConfig(stdout_level=LogLevel.OFF),
            data_engine=DataEngineConfig(time_bars_build_with_no_updates=False),
            run_analysis=False,
        )
    )
    usd = Currency.from_str("USD")
    fee = (
        config["stress_commission_per_order"]
        if job.get("stress")
        else config["commission_per_order"]
    )
    engine.add_venue(
        venue=instrument.id.venue,
        oms_type=OmsType.NETTING,
        account_type=AccountType.CASH,
        base_currency=usd,
        starting_balances=[Money(config["capital"], usd)],
        fee_model=FixedFeeModel(Money(fee, usd)),
        fill_model=OneTickSlippageFillModel(),
        latency_model=StaticLatencyModel(base_latency_nanos=1),
    )
    try:
        engine.add_instrument(instrument)
        engine.add_data(bars)
        engine.add_strategy(strategy)
        engine.run()
        if errors:
            raise ValueError("Strategy callback failed: " + errors[0])
        if getattr(module, "FLAT_AT_END", False) and not engine.portfolio.is_net_flat(
            instrument.id
        ):
            raise ValueError("Intraday strategy left a position open at the window end")
        orders = engine.generate_orders_report()
        if (
            len(orders)
            and orders["status"].astype(str).str.contains("REJECTED|DENIED").any()
        ):
            rejected = orders[
                orders["status"].astype(str).str.contains("REJECTED|DENIED")
            ]
            raise ValueError(f"Strategy order failed: {rejected.iloc[0].to_dict()}")
        return measure(engine, bars, config["capital"])
    finally:
        engine.dispose()


def main():
    from pathlib import Path

    try:
        job = json.load(sys.stdin)
        config = job["config"]
        seconds = int(config["timeout_seconds"])
        resource.setrlimit(resource.RLIMIT_CPU, (seconds, seconds))
        if sys.platform == "linux":
            memory = int(config["memory_mb"] * 1024**2)
            resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
        module = load(Path(job["path"]))
        result = (
            describe(module, config)
            if job["action"] == "describe"
            else backtest(module, job)
        )
        print(json.dumps(result, allow_nan=False))
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
        sys.exit(1)


if __name__ == "__main__":
    main()
