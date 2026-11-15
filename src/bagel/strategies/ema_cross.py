"""EMA crossover: the classic trend-following starter.

Concepts introduced:
- technical indicators (``ExponentialMovingAverage``)
- auto-updating indicators from a bar stream (``register_indicator_for_bars``)
- waiting for indicators to warm up (``indicator.initialized``)
- going long / flat on a signal, one position at a time (NETTING)

Rule: when the fast EMA is above the slow EMA, be long; otherwise be flat.
"""

from __future__ import annotations

from nautilus_trader.indicators import ExponentialMovingAverage
from nautilus_trader.model import BarType, InstrumentId, OrderSide, Quantity

from bagel.strategies.base import BaseStrategy


class EmaCross(BaseStrategy):
    def __init__(
        self,
        instrument_id: InstrumentId,
        bar_type: BarType,
        fast_period: int = 10,
        slow_period: int = 30,
        trade_size: int = 100,
    ) -> None:
        super().__init__()
        self._instrument_id = instrument_id
        self._bar_type = bar_type
        self._qty = Quantity.from_int(trade_size)
        self._fast = ExponentialMovingAverage(fast_period)
        self._slow = ExponentialMovingAverage(slow_period)

    def on_start(self) -> None:
        self.register_indicator_for_bars(self._bar_type, self._fast)
        self.register_indicator_for_bars(self._bar_type, self._slow)
        self.subscribe_bars(self._bar_type)

    def on_bar(self, bar) -> None:
        # Wait until both EMAs have enough data to be meaningful.
        if not (self._fast.initialized and self._slow.initialized):
            return

        is_flat = self.portfolio.is_net_flat(self._instrument_id)
        if self._fast.value > self._slow.value and is_flat:
            self.submit_order(
                self.order_factory.market(self._instrument_id, OrderSide.BUY, self._qty)
            )
        elif self._fast.value < self._slow.value and not is_flat:
            self.close_all_positions(self._instrument_id)
