"""Multi-timeframe EMA cross on bars rebuilt from the 1-minute base.

This is the whole reason we stored 1-minute data: instead of downloading 5- or
15-minute bars separately, we subscribe to a *composite* bar type and let
Nautilus aggregate them internally from the 1-minute bars in the catalog.

Concepts introduced:
- composite bar types (``N-MINUTE-...-INTERNAL@1-MINUTE-EXTERNAL``)
- a strategy reacting to a timeframe it never had to store

The trading rule is the same EMA cross as ``ema_cross`` — the point here is the
*data plumbing*, i.e. trading a higher timeframe derived on the fly.
"""

from __future__ import annotations

from nautilus_trader.indicators import ExponentialMovingAverage
from nautilus_trader.model import InstrumentId, OrderSide, Quantity

from bagel.strategies.base import BaseStrategy
from bagel.universe import base_bar_type, composite_bar_type


class MultiTimeframeEma(BaseStrategy):
    def __init__(
        self,
        instrument_id: InstrumentId,
        minutes: int = 15,
        fast_period: int = 10,
        slow_period: int = 30,
        trade_size: int = 100,
    ) -> None:
        super().__init__()
        self._instrument_id = instrument_id
        # Higher timeframe aggregated from the stored 1-minute bars.
        self._bar_type = composite_bar_type(instrument_id, minutes)
        # ...which means the engine must be fed the 1-minute base series.
        self._data_bar_type = base_bar_type(instrument_id)
        self._qty = Quantity.from_int(trade_size)
        self._fast = ExponentialMovingAverage(fast_period)
        self._slow = ExponentialMovingAverage(slow_period)

    def on_start(self) -> None:
        self.register_indicator_for_bars(self._bar_type, self._fast)
        self.register_indicator_for_bars(self._bar_type, self._slow)
        self.subscribe_bars(self._bar_type)

    def on_bar(self, bar) -> None:
        if not (self._fast.initialized and self._slow.initialized):
            return

        is_flat = self.portfolio.is_net_flat(self._instrument_id)
        if self._fast.value > self._slow.value and is_flat:
            self.submit_order(
                self.order_factory.market(self._instrument_id, OrderSide.BUY, self._qty)
            )
        elif self._fast.value < self._slow.value and not is_flat:
            self.close_all_positions(self._instrument_id)
