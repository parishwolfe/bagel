"""SMA trend filter: hold while price is above a moving average, else flat.

A different idea from the EMA *cross*: here a single moving average acts as a
regime filter. The classic use is the 200-day SMA on leveraged ETFs (UPRO /
TQQQ) — stay invested only while the long-term trend is up. Defaults to the
**daily** series.

Concepts introduced:
- using one indicator as a regime/trend filter (price vs MA)
- a strategy whose natural timeframe is daily, not intraday
"""

from __future__ import annotations

from nautilus_trader.indicators import SimpleMovingAverage
from nautilus_trader.model import BarType, InstrumentId, OrderSide, Quantity

from bagel.strategies.base import BaseStrategy
from bagel.universe import daily_bar_type


class SmaTrend(BaseStrategy):
    def __init__(
        self,
        instrument_id: InstrumentId,
        bar_type: BarType | None = None,
        period: int = 200,
        trade_size: int = 100,
    ) -> None:
        super().__init__()
        self._instrument_id = instrument_id
        self._bar_type = bar_type or daily_bar_type(instrument_id)
        self._qty = Quantity.from_int(trade_size)
        self._sma = SimpleMovingAverage(period)

    def on_start(self) -> None:
        self.register_indicator_for_bars(self._bar_type, self._sma)
        self.subscribe_bars(self._bar_type)

    def on_bar(self, bar) -> None:
        if not self._sma.initialized:
            return
        price = float(bar.close)
        is_flat = self.portfolio.is_net_flat(self._instrument_id)
        if price > self._sma.value and is_flat:
            self.submit_order(
                self.order_factory.market(self._instrument_id, OrderSide.BUY, self._qty)
            )
        elif price < self._sma.value and not is_flat:
            self.close_all_positions(self._instrument_id)
