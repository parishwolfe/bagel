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
    Venue,
)

from bagel.config import settings
from bagel.ingest.catalog import open_catalog
from bagel.strategies import (
    BuyAndHold,
    DonchianBreakout,
    EmaCross,
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
)


def _build_strategy(name: str, instrument_id: InstrumentId, **params):
    size = params.get("trade_size", 100)
    if name == "buy_and_hold":
        return BuyAndHold(instrument_id, base_bar_type(instrument_id), trade_size=size)
    if name == "ema_cross":
        return EmaCross(
            instrument_id,
            base_bar_type(instrument_id),
            fast_period=params.get("fast", 10),
            slow_period=params.get("slow", 30),
            trade_size=size,
        )
    if name == "multi_tf":
        return MultiTimeframeEma(
            instrument_id,
            minutes=params.get("minutes", 15),
            fast_period=params.get("fast", 10),
            slow_period=params.get("slow", 30),
            trade_size=size,
        )
    if name == "sma_trend":  # daily by default
        return SmaTrend(
            instrument_id, period=params.get("period", 200), trade_size=size
        )
    if name == "rsi_reversion":  # daily by default
        return RsiReversion(
            instrument_id,
            period=params.get("period", 14),
            lower=params.get("lower", 30.0),
            upper=params.get("upper", 70.0),
            trade_size=size,
        )
    if name == "donchian_breakout":  # daily by default
        return DonchianBreakout(
            instrument_id, period=params.get("period", 20), trade_size=size
        )
    raise ValueError(f"unknown strategy '{name}'")


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
    instrument_id = InstrumentId.from_str(f"{symbol}.{VENUE}")
    sid = str(instrument_id)

    instruments = catalog.instruments(instrument_ids=[sid])
    if not instruments:
        raise RuntimeError(
            f"No instrument for {sid} in catalog — run the downloader first."
        )

    # Build the strategy first so we know which stored series to feed the engine
    # (1-minute for intraday/composite strategies, daily for the daily ones).
    strat = _build_strategy(strategy, instrument_id, **params)
    data_bar_type = str(strat.data_bar_type)
    bars = catalog.query_bars(identifiers=[data_bar_type])
    if not bars:
        raise RuntimeError(
            f"No {data_bar_type} bars in catalog — run the matching downloader "
            f"(e.g. `bagel-download --timeframe "
            f"{'1day' if '1-DAY' in data_bar_type else '1min'}`)."
        )

    engine = BacktestEngine(
        config=BacktestEngineConfig(logging=LoggerConfig(stdout_level=LogLevel.ERROR))
    )
    engine.add_venue(
        venue=Venue(VENUE),
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        base_currency=USD,
        starting_balances=[Money(settings.starting_capital, USD)],
        fee_model=FixedFeeModel(Money(0, USD)),
    )
    engine.add_instrument(instruments[0])
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
    parser.add_argument("symbol", help="e.g. SPY, UPRO, TQQQ")
    parser.add_argument("--fast", type=int, default=10, help="ema_cross / multi_tf")
    parser.add_argument("--slow", type=int, default=30, help="ema_cross / multi_tf")
    parser.add_argument("--minutes", type=int, default=15, help="multi_tf timeframe")
    parser.add_argument(
        "--period", type=int, default=None, help="sma / rsi / donchian lookback"
    )
    parser.add_argument("--lower", type=float, default=30.0, help="rsi oversold")
    parser.add_argument("--upper", type=float, default=70.0, help="rsi overbought")
    parser.add_argument("--trade-size", type=int, default=100)
    args = parser.parse_args()

    params = dict(
        fast=args.fast,
        slow=args.slow,
        minutes=args.minutes,
        lower=args.lower,
        upper=args.upper,
        trade_size=args.trade_size,
    )
    if args.period is not None:
        params["period"] = args.period
    run_backtest(args.strategy, args.symbol.upper(), **params)


if __name__ == "__main__":
    main()
