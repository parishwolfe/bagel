"""Run one backtest of a generated strategy and measure it.

Differences from ``bagel.backtest.run`` (which is left untouched):

* any number of instruments in one run (pairs, rotation);
* positions sized against account equity instead of a fixed share count;
* commission and slippage on every fill;
* results measured on a mark-to-market daily equity curve built from the fills,
  annualised, and compared with buying and holding the same instruments;
* data is addressed by window name only ("smoke", "train", "validate"), so
  nothing here can read the vault period after ``validate_end``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import math
import signal
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from nautilus_trader.backtest import BacktestEngine, BacktestEngineConfig
from nautilus_trader.common import LogLevel
from nautilus_trader.config import DataEngineConfig, LoggerConfig
from nautilus_trader.execution import (
    FixedFeeModel,
    OneTickSlippageFillModel,
    PerContractFeeModel,
)
from nautilus_trader.model import AccountType, Money, OmsType

from bagel.config import settings as bagel_settings
from bagel.ingest.catalog import open_catalog
from bagel.universe import VENUE
from lox import lint
from lox.api import NEW_YORK, USD, Deadline, LoxStrategy, RunContext
from lox.settings import Costs, load_settings

BAR_SPECS = {"minute": "1-MINUTE-LAST-EXTERNAL", "daily": "1-DAY-LAST-EXTERNAL"}
WINDOWS = ("smoke", "train", "validate")
DAY_NS = 86_400 * 1_000_000_000


class StrategyError(Exception):
    """The strategy could not be loaded or run; the message is shown to the coder."""


# --- data inventory -------------------------------------------------------------


def inventory() -> list[dict]:
    """What the catalog holds, read from its directory layout (no data loaded)."""
    root = bagel_settings.catalog_path / "data"
    rows: list[dict] = []
    for folder in sorted((root / "bars").glob("*")):
        instrument, _, spec = folder.name.partition("-")
        timeframe = next((k for k, v in BAR_SPECS.items() if v == spec), None)
        files = [f.stem.partition("_") for f in folder.glob("*.parquet")]
        if timeframe is None or not files:
            continue
        symbol, _, venue = instrument.partition(".")
        if (root / "equity" / instrument).exists():
            kind = "equity"
        elif (root / "futures_contract" / instrument).exists():
            kind = "futures"
        else:
            kind = "other"
        rows.append(
            {
                "symbol": symbol,
                "venue": venue,
                "kind": kind,
                "timeframe": timeframe,
                "start": min(f[0][:10] for f in files),
                "end": max(f[2][:10] for f in files),
                # v1 trades equities only: one ES contract is ~6x a $50k account.
                "tradable": kind == "equity" and venue == VENUE,
            }
        )
    return rows


def tradable_symbols(timeframe: str) -> set[str]:
    return {
        r["symbol"]
        for r in inventory()
        if r["tradable"] and r["timeframe"] == timeframe
    }


# --- windows ----------------------------------------------------------------------


def _ns(date: str, plus_days: int = 0) -> int:
    when = dt.datetime.fromisoformat(date).replace(tzinfo=dt.UTC)
    return int((when + dt.timedelta(days=plus_days)).timestamp()) * 1_000_000_000


def window_bounds(timeframe: str, window: str) -> tuple[int, int]:
    """[start, end) of a named window in nanoseconds. End dates are inclusive."""
    settings = load_settings()
    w = settings.windows(timeframe)
    if window == "train":
        return _ns(w.train_start), _ns(w.train_end, 1)
    if window == "validate":
        return _ns(w.validate_start), _ns(w.validate_end, 1)
    if window == "smoke":
        days = int(settings.section("smoke")[f"{timeframe}_days"])
        return _ns(w.train_start), min(_ns(w.train_start, days), _ns(w.train_end, 1))
    raise ValueError(f"window must be one of {', '.join(WINDOWS)}")


# --- per-process caches --------------------------------------------------------------

_catalog = None
_series_cache: dict = {}
_marks_cache: dict = {}
_instrument_cache: dict = {}
_module_cache: dict = {}


def _get_catalog():
    global _catalog
    if _catalog is None:
        _catalog = open_catalog()
    return _catalog


def _series(bar_type: str):
    hit = _series_cache.get(bar_type)
    if hit is None:
        bars = _get_catalog().query_bars(identifiers=[bar_type])
        stamps = np.fromiter(
            (b.ts_event for b in bars), dtype=np.int64, count=len(bars)
        )
        hit = _series_cache[bar_type] = (bars, stamps)
    return hit


def _instrument(instrument_id: str):
    hit = _instrument_cache.get(instrument_id)
    if hit is None:
        found = _get_catalog().instruments(instrument_ids=[instrument_id])
        if not found:
            raise StrategyError(f"no instrument {instrument_id} in the catalog")
        hit = _instrument_cache[instrument_id] = found[0]
    return hit


@dataclass(frozen=True)
class Marks:
    closes: pd.Series  # last close per New York date
    first_open: float


def _marks(bar_type: str, lo: int, hi: int) -> Marks:
    key = (bar_type, lo, hi)
    hit = _marks_cache.get(key)
    if hit is None:
        bars, stamps = _series(bar_type)
        closes = np.fromiter(
            (float(b.close) for b in bars[lo:hi]), dtype=float, count=hi - lo
        )
        days = pd.to_datetime(stamps[lo:hi], utc=True).tz_convert(NEW_YORK).date
        hit = _marks_cache[key] = Marks(
            closes=pd.Series(closes, index=days).groupby(level=0).last(),
            first_open=float(bars[lo].open),
        )
    return hit


def load_strategy_class(path: Path, class_name: str) -> type[LoxStrategy]:
    """Lint, then import, a strategy file and return one of its classes."""
    path = Path(path).resolve()
    stat = path.stat() if path.exists() else None
    if stat is None:
        raise StrategyError(f"{path} does not exist")
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    module = _module_cache.get(key)
    if module is None:
        problems = lint.check_file(path)
        if problems:
            raise StrategyError("lint failed:\n  " + "\n  ".join(problems[:20]))
        name = "lox_generated_" + hashlib.sha1(repr(key).encode()).hexdigest()[:12]
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        previous = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            tail = traceback.format_exc(limit=3)
            raise StrategyError(
                f"import failed: {type(exc).__name__}: {exc}\n{tail}"
            ) from exc
        finally:
            sys.dont_write_bytecode = previous
        _module_cache[key] = module
    cls = getattr(module, class_name, None)
    if not (
        isinstance(cls, type)
        and issubclass(cls, LoxStrategy)
        and cls is not LoxStrategy
    ):
        raise StrategyError(
            f"{path.name} has no LoxStrategy subclass named {class_name}"
        )
    return cls


# --- costs and time limit ----------------------------------------------------------------


def _fee_model(costs: Costs):
    if costs.commission_per_share > 0:
        cents = math.ceil(costs.commission_per_share * 100 - 1e-9) / 100
        return PerContractFeeModel(Money(cents, USD))
    return FixedFeeModel(Money(costs.commission_per_order, USD))


def _arm_timer(seconds: float | None, state: dict) -> None:
    if not seconds or threading.current_thread() is not threading.main_thread():
        return

    def on_alarm(signum, frame):
        state["deadline"] = True
        if state.get("in_run"):
            raise Deadline

    signal.signal(signal.SIGALRM, on_alarm)
    signal.setitimer(signal.ITIMER_REAL, seconds, 0.5)


def _clear_timer() -> None:
    if threading.current_thread() is threading.main_thread():
        signal.setitimer(signal.ITIMER_REAL, 0)


# --- measurement ----------------------------------------------------------------------------


def _money(series: pd.Series) -> np.ndarray:
    text = series.astype(str).str.split().str[0].str.replace(",", "", regex=False)
    return text.astype(float).to_numpy()


def _annualise(total: float, years: float) -> float:
    if total <= -1.0:
        return -1.0
    return float(min((1.0 + total) ** (1.0 / years) - 1.0, 1e6))


def measure(fills, positions, marks: dict[str, Marks], capital: float) -> dict:
    """Turn fills into a daily mark-to-market equity curve and summary numbers."""
    days = sorted(set().union(*[m.closes.index for m in marks.values()]))
    index = pd.Index(days)
    closes = {s: m.closes.reindex(index).ffill().bfill() for s, m in marks.items()}
    span_years = max((days[-1] - days[0]).days + 1, 1) / 365.25

    equity = pd.Series(float(capital), index=index)
    max_leverage = 0.0
    n_fills = 0 if fills is None else len(fills)
    if n_fills:
        f = fills.sort_values("ts_event", kind="stable")
        qty = f["last_qty"].astype(float).to_numpy()
        price = f["last_px"].astype(float).to_numpy()
        buys = f["order_side"].astype(str).str.upper().str.contains("BUY").to_numpy()
        signed = np.where(buys, qty, -qty)
        fees = _money(f["commission"])
        symbols = f["instrument_id"].astype(str).str.split(".").str[0].to_numpy()
        when = pd.to_datetime(f["ts_event"], utc=True).dt.tz_convert(NEW_YORK)
        fill_days = np.array(when.dt.date)
        cash_flow = -(signed * price) - fees

        cash = (
            pd.Series(cash_flow).groupby(fill_days).sum().reindex(index, fill_value=0.0)
        )
        equity = float(capital) + cash.cumsum()
        for symbol, close in closes.items():
            mask = symbols == symbol
            held = pd.Series(signed[mask]).groupby(fill_days[mask]).sum()
            equity = equity + held.reindex(index, fill_value=0.0).cumsum() * close

        # Gross exposure over equity, checked at every fill.
        held_now: dict[str, float] = {}
        last_px: dict[str, float] = {}
        running_cash = float(capital)
        for symbol, delta, px, flow in zip(
            symbols, signed, price, cash_flow, strict=True
        ):
            held_now[symbol] = held_now.get(symbol, 0.0) + delta
            last_px[symbol] = px
            running_cash += flow
            gross = sum(abs(q) * last_px[s] for s, q in held_now.items())
            net = running_cash + sum(q * last_px[s] for s, q in held_now.items())
            if net > 0:
                max_leverage = max(max_leverage, gross / net)
            elif gross > 0:
                max_leverage = float("inf")

    curve = np.concatenate([[float(capital)], equity.to_numpy()])
    returns = curve[1:] / curve[:-1] - 1.0
    total = float(curve[-1] / capital - 1.0)
    peak = np.maximum.accumulate(curve)
    spread = float(returns.std())

    trades = wins = 0
    gross_win = gross_loss = 0.0
    if positions is not None and len(positions) and "ts_closed" in positions:
        closed = positions[positions["ts_closed"].notna()]
        pnl = _money(closed["realized_pnl"])
        trades = int(len(pnl))
        wins = int((pnl > 0).sum())
        gross_win = float(pnl[pnl > 0].sum())
        gross_loss = float(-pnl[pnl < 0].sum())

    bench = float(
        np.mean([m.closes.iloc[-1] / m.first_open - 1.0 for m in marks.values()])
    )
    return {
        "fills": int(n_fills),
        "trades": trades,
        "win_rate": round(wins / trades, 4) if trades else 0.0,
        "profit_factor": round(gross_win / gross_loss, 4) if gross_loss > 0 else None,
        "total_return": round(total, 6),
        "annual_return": round(_annualise(total, span_years), 6),
        "max_drawdown": round(float(((curve - peak) / peak).min()), 6),
        "sharpe": round(float(returns.mean() / spread * math.sqrt(252)), 4)
        if spread > 0
        else 0.0,
        "final_equity": round(float(curve[-1]), 2),
        "max_leverage": round(float(max_leverage), 4)
        if math.isfinite(max_leverage)
        else 999.0,
        "benchmark_total_return": round(bench, 6),
        "benchmark_annual_return": round(_annualise(bench, span_years), 6),
        "days": len(days),
    }


# --- one run ------------------------------------------------------------------------------


def run_backtest(
    *,
    strategy_path: Path,
    class_name: str,
    params: dict,
    symbols: list[str],
    timeframe: str,
    window: str,
    costs: str = "base",
    warmup_days: int = 0,
    timeout: float | None = None,
) -> dict:
    """Run one configuration. Always returns a dict with a ``status``:

    ``ok``, ``error`` (the strategy raised or failed lint), ``timeout``, or
    ``leverage`` (gross exposure went above the account's limit).
    """
    started = time.time()
    state = {"deadline": False, "in_run": False}
    try:
        result = _run(
            Path(strategy_path), class_name, params, symbols, timeframe, window,
            costs, warmup_days, timeout, state,
        )  # fmt: skip
    except Deadline:
        result = {"status": "timeout", "error": f"exceeded the {timeout:g}s limit"}
    except StrategyError as exc:
        result = {"status": "error", "error": str(exc)}
    except Exception as exc:
        result = {
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=4)}",
        }
    finally:
        state["in_run"] = False
        _clear_timer()
    result["elapsed"] = round(time.time() - started, 3)
    return result


def _run(
    path,
    class_name,
    params,
    symbols,
    timeframe,
    window,
    costs,
    warmup_days,
    timeout,
    state,
):
    settings = load_settings()
    if timeframe not in BAR_SPECS:
        raise StrategyError(f"timeframe must be one of {', '.join(BAR_SPECS)}")
    cls = load_strategy_class(path, class_name)
    start_ns, end_ns = window_bounds(timeframe, window)
    data_start = start_ns - int(warmup_days) * DAY_NS

    instruments, data, marks = {}, [], {}
    for symbol in symbols:
        instrument = _instrument(f"{symbol}.{VENUE}")
        bar_type = f"{instrument.id}-{BAR_SPECS[timeframe]}"
        bars, stamps = _series(bar_type)
        lo, mid, hi = (
            int(np.searchsorted(stamps, ns)) for ns in (data_start, start_ns, end_ns)
        )
        if hi - mid < 2:
            raise StrategyError(
                f"no {timeframe} bars for {symbol} in the {window} window"
            )
        instruments[symbol] = instrument
        data.append(bars[lo:hi])
        marks[symbol] = _marks(bar_type, mid, hi)

    ctx = RunContext(
        params=dict(params),
        symbols=list(symbols),
        instrument_ids={s: i.id for s, i in instruments.items()},
        timeframe=timeframe,
        capital=settings.capital,
        max_leverage=settings.max_leverage,
        trade_start_ns=start_ns,
        state=state,
    )
    cost = settings.costs(costs)
    _arm_timer(timeout, state)
    state["in_run"] = True
    strategy = cls(ctx)
    if strategy._lox["errors"]:
        raise StrategyError("setup() raised:\n" + (strategy._lox["first_error"] or ""))

    engine = BacktestEngine(
        config=BacktestEngineConfig(
            logging=LoggerConfig(stdout_level=LogLevel.OFF),
            # Without this, aggregated bars (5-minute and so on) keep printing all
            # night with no trades behind them, which flattens every indicator.
            data_engine=DataEngineConfig(time_bars_build_with_no_updates=False),
            run_analysis=False,  # lox measures results itself, from the fills
        )
    )
    venue_options = {}
    if cost.slippage_ticks >= 1:
        venue_options["fill_model"] = OneTickSlippageFillModel()
    engine.add_venue(
        venue=next(iter(instruments.values())).id.venue,
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        base_currency=USD,
        starting_balances=[Money(settings.capital, USD)],
        fee_model=_fee_model(cost),
        **venue_options,
    )
    for symbol, bars in zip(symbols, data, strict=True):
        engine.add_instrument(instruments[symbol])
        engine.add_data(bars)
    engine.add_strategy(strategy)
    try:
        engine.run()
    finally:
        state["in_run"] = False
        _clear_timer()

    lox = strategy._lox
    if state["deadline"]:
        engine.dispose()
        raise Deadline
    if lox["errors"]:
        engine.dispose()
        raise StrategyError(
            "the strategy raised during the run:\n" + (lox["first_error"] or "")
        )

    fills = engine.generate_fills_report()
    positions = engine.generate_positions_report()
    engine.dispose()

    result = measure(fills, positions, marks, settings.capital)
    result["bars"] = int(sum(len(b) for b in data))
    result["dropped_orders"] = int(lox["dropped_orders"])
    if result["max_leverage"] > settings.max_leverage * 1.02:
        result["status"] = "leverage"
        result["error"] = (
            f"gross exposure reached {result['max_leverage']:.2f}x equity; the limit is "
            f"{settings.max_leverage:g}x. Size every order with shares_for()."
        )
    else:
        result["status"] = "ok"
        result["error"] = None
    return result
