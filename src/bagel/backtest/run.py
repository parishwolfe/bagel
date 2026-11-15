"""Run one strategy over one symbol's catalog data and print a summary.

Wires together the pieces: read instrument + 1-minute bars from the catalog,
stand up a simulated ARCA venue (zero-fee, margin, USD), attach the strategy,
run, and report PnL / trades. The 1-minute bars are always the data source;
``multi_tf`` has Nautilus aggregate higher timeframes from them internally.
"""

from __future__ import annotations

from dataclasses import dataclass

from nautilus_trader.backtest import BacktestEngine, BacktestEngineConfig
from nautilus_trader.common import LogLevel
from nautilus_trader.config import LoggerConfig
from nautilus_trader.execution import FixedFeeModel
from nautilus_trader.model import (
    AccountType,
    Currency,
    InstrumentId,
    Money,
    OmsType,
)

from bagel.config import settings
from bagel.futures import es_bar_type
from bagel.ingest.catalog import open_catalog
from bagel.strategies import (
    BuyAndHold,
    DonchianBreakout,
    EmaCross,
    EsTrend,
    MultiTimeframeEma,
    RsiReversion,
    SmaTrend,
)
from bagel.universe import VENUE, base_bar_type

USD = Currency.from_str("USD")

STRATEGIES = (
    "buy_and_hold",
    "ema_cross",
    "multi_tf",
    "sma_trend",
    "rsi_reversion",
    "donchian_breakout",
    "es_trend",
)


def _build_strategy(name: str, instrument_id: InstrumentId, **params):
    size = params.get("trade_size")  # None -> each strategy applies its own default
    if name == "buy_and_hold":
        return BuyAndHold(
            instrument_id, base_bar_type(instrument_id), trade_size=size or 100
        )
    if name == "ema_cross":
        return EmaCross(
            instrument_id,
            base_bar_type(instrument_id),
            fast_period=params.get("fast", 10),
            slow_period=params.get("slow", 30),
            trade_size=size or 100,
        )
    if name == "multi_tf":
        return MultiTimeframeEma(
            instrument_id,
            minutes=params.get("minutes", 15),
            fast_period=params.get("fast", 10),
            slow_period=params.get("slow", 30),
            trade_size=size or 100,
        )
    if name == "sma_trend":  # daily by default
        return SmaTrend(
            instrument_id, period=params.get("period", 200), trade_size=size or 100
        )
    if name == "rsi_reversion":  # daily by default
        return RsiReversion(
            instrument_id,
            period=params.get("period", 14),
            lower=params.get("lower", 30.0),
            upper=params.get("upper", 70.0),
            trade_size=size or 100,
        )
    if name == "donchian_breakout":  # daily by default
        return DonchianBreakout(
            instrument_id, period=params.get("period", 20), trade_size=size or 100
        )
    if name == "es_trend":  # ES futures, long/short, sized in contracts
        return EsTrend(
            instrument_id,
            bar_type=es_bar_type(params.get("timeframe", "1day")),
            fast_period=params.get("fast", 20),
            slow_period=params.get("slow", 50),
            contracts=size or 1,
        )
    raise ValueError(f"unknown strategy '{name}'")


def _resolve_instrument(catalog, symbol: str):
    """Find the catalog instrument for ``symbol`` across venues.

    ETFs live on ``ARCA`` (fast path); ES futures live on ``XCME``. Scanning the
    catalog keeps the runner venue-agnostic instead of hard-coding ARCA.
    """
    direct = catalog.instruments(instrument_ids=[f"{symbol}.{VENUE}"])
    if direct:
        return direct[0]
    for inst in catalog.instruments():
        if str(inst.id.symbol) == symbol:
            return inst
    return None


def _money(value) -> float:
    """Parse a Nautilus money string like '1,000,123.45 USD' -> float."""
    return float(str(value).split()[0].replace(",", ""))


@dataclass
class Summary:
    strategy: str
    symbol: str
    bars: int
    trades: int
    wins: int
    realized_pnl: float
    net_pnl: float  # realized + unrealized (mark-to-market, incl. open position)
    start_equity: float

    @property
    def return_pct(self) -> float:
        """Mark-to-market return, so a still-open position (e.g. buy-and-hold) counts."""
        return (self.net_pnl / self.start_equity) * 100.0

    @property
    def win_rate(self) -> float:
        return (self.wins / self.trades * 100.0) if self.trades else 0.0

    def render(self) -> str:
        return (
            f"\n=== {self.strategy} on {self.symbol} ===\n"
            f"  bars processed : {self.bars:,}\n"
            f"  closed trades  : {self.trades}  (win rate {self.win_rate:.1f}%)\n"
            f"  realized PnL   : ${self.realized_pnl:,.2f}\n"
            f"  net PnL (MtM)  : ${self.net_pnl:,.2f}\n"
            f"  start equity   : ${self.start_equity:,.2f}\n"
            f"  net return     : {self.return_pct:+.2f}%\n"
        )


def run_backtest(strategy: str, symbol: str, **params) -> Summary:
    catalog = open_catalog()
    instrument = _resolve_instrument(catalog, symbol)
    if instrument is None:
        raise RuntimeError(
            f"No instrument for {symbol} in catalog — run the matching ingest "
            f"(`bagel-download` for ETFs, `bagel-firstrate` for ES)."
        )
    instrument_id = instrument.id

    # Build the strategy first so we know which stored series to feed the engine
    # (1-minute for intraday/composite strategies, daily for the daily ones).
    strat = _build_strategy(strategy, instrument_id, **params)
    data_bar_type = str(strat.data_bar_type)
    bars = catalog.query_bars(identifiers=[data_bar_type])
    if not bars:
        is_futures = str(instrument_id.venue) == "XCME"
        hint = (
            f"bagel-firstrate --timeframe {'1day' if '1-DAY' in data_bar_type else '1min'}"
            if is_futures
            else f"bagel-download --timeframe {'1day' if '1-DAY' in data_bar_type else '1min'}"
        )
        raise RuntimeError(f"No {data_bar_type} bars in catalog — run `{hint}`.")

    engine = BacktestEngine(
        config=BacktestEngineConfig(logging=LoggerConfig(stdout_level=LogLevel.ERROR))
    )
    engine.add_venue(
        venue=instrument_id.venue,  # ARCA for ETFs, XCME for ES futures
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        base_currency=USD,
        starting_balances=[Money(settings.starting_capital, USD)],
        fee_model=FixedFeeModel(Money(0, USD)),
    )
    engine.add_instrument(instrument)
    engine.add_data(bars)
    engine.add_strategy(strat)
    engine.run()

    positions = engine.generate_positions_report()

    trades = len(positions)
    realized = 0.0
    wins = 0
    if trades:
        pnls = positions["realized_pnl"].map(_money)
        realized = float(pnls.sum())
        wins = int((pnls > 0).sum())

    # Mark-to-market: realized + unrealized of any still-open position.
    try:
        net = engine.portfolio.total_pnl(instrument_id)
        net_pnl = _money(net) if net is not None else realized
    except Exception:
        net_pnl = realized

    summary = Summary(
        strategy=strategy,
        symbol=symbol,
        bars=len(bars),
        trades=trades,
        wins=wins,
        realized_pnl=realized,
        net_pnl=net_pnl,
        start_equity=float(settings.starting_capital),
    )
    print(summary.render())
    return summary


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Backtest a strategy over catalog data"
    )
    parser.add_argument("strategy", choices=list(STRATEGIES))
    parser.add_argument("symbol", help="e.g. SPY, UPRO, TQQQ, ES")
    parser.add_argument(
        "--fast", type=int, default=None, help="ema_cross / multi_tf / es_trend"
    )
    parser.add_argument(
        "--slow", type=int, default=None, help="ema_cross / multi_tf / es_trend"
    )
    parser.add_argument("--minutes", type=int, default=15, help="multi_tf timeframe")
    parser.add_argument(
        "--timeframe",
        choices=["1day", "1min"],
        default="1day",
        help="es_trend data series (1day default, or 1min)",
    )
    parser.add_argument(
        "--period", type=int, default=None, help="sma / rsi / donchian lookback"
    )
    parser.add_argument("--lower", type=float, default=30.0, help="rsi oversold")
    parser.add_argument("--upper", type=float, default=70.0, help="rsi overbought")
    parser.add_argument(
        "--trade-size",
        type=int,
        default=None,
        help="shares (ETF strategies) or contracts (es_trend); per-strategy default",
    )
    args = parser.parse_args()

    # Only forward explicitly-set values so each strategy keeps its own defaults.
    params = dict(
        minutes=args.minutes,
        lower=args.lower,
        upper=args.upper,
        timeframe=args.timeframe,
    )
    for key in ("fast", "slow", "period", "trade_size"):
        value = getattr(args, key)
        if value is not None:
            params[key] = value
    run_backtest(args.strategy, args.symbol.upper(), **params)


if __name__ == "__main__":
    main()
