"""ES futures trend: EMA cross that goes long **and** short.

The sample strategy for the FirstRate ES continuous data. It is deliberately
*futures-flavoured* and different from the equity strategies in two ways:

1. **Long and short.** Shorting a future is as natural as going long (no borrow,
   symmetric margin), so on a golden cross we go long and on a death cross we
   flip to short — rather than the long/flat pattern the ETF strategies use.
2. **Sized in contracts, not shares.** One ES contract carries a ``$50``/point
   multiplier (see :mod:`bagel.futures`), so a single contract already controls
   roughly ``$`` ``200k`` of notional near a ``4000`` index level. The default
   size is therefore **1 contract**, and PnL is ``points * 50 * contracts``.

Runs on the **daily** continuous series by default. Remember the series is
back-adjusted: use it for signals, not for absolute price levels.
"""

from __future__ import annotations

from nautilus_trader.indicators import ExponentialMovingAverage
from nautilus_trader.model import BarType, InstrumentId, OrderSide, Quantity

from bagel.futures import es_bar_type
from bagel.strategies.base import BaseStrategy


class EsTrend(BaseStrategy):
    def __init__(
        self,
        instrument_id: InstrumentId,
        bar_type: BarType | None = None,
        fast_period: int = 20,
        slow_period: int = 50,
        contracts: int = 1,
    ) -> None:
        super().__init__()
        self._instrument_id = instrument_id
        self._bar_type = bar_type or es_bar_type("1day")
        self._qty = Quantity.from_int(contracts)
        self._fast = ExponentialMovingAverage(fast_period)
        self._slow = ExponentialMovingAverage(slow_period)

    def on_start(self) -> None:
        self.register_indicator_for_bars(self._bar_type, self._fast)
        self.register_indicator_for_bars(self._bar_type, self._slow)
        self.subscribe_bars(self._bar_type)

    def on_bar(self, bar) -> None:
        if not (self._fast.initialized and self._slow.initialized):
            return

        if self._fast.value > self._slow.value:
            # Uptrend: want to be long. Close any short, then go long if flat.
            if self.portfolio.is_net_short(self._instrument_id):
                self.close_all_positions(self._instrument_id)
            if self.portfolio.is_net_flat(self._instrument_id):
                self.submit_order(
                    self.order_factory.market(
                        self._instrument_id, OrderSide.BUY, self._qty
                    )
                )
        elif self._fast.value < self._slow.value:
            # Downtrend: want to be short. Close any long, then go short if flat.
            if self.portfolio.is_net_long(self._instrument_id):
                self.close_all_positions(self._instrument_id)
            if self.portfolio.is_net_flat(self._instrument_id):
                self.submit_order(
                    self.order_factory.market(
                        self._instrument_id, OrderSide.SELL, self._qty
                    )
                )
