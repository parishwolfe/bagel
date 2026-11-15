"""RSI mean-reversion: buy oversold, exit when it snaps back.

A *mean-reversion* idea, opposite in spirit to the trend-following strategies:
enter long when RSI drops below an oversold threshold, exit when it recovers
past an overbought one. Defaults to the **daily** series.

Concepts introduced:
- an oscillator indicator (RSI) with entry/exit thresholds
- contrarian entries rather than trend-following
"""

from __future__ import annotations

from nautilus_trader.indicators import RelativeStrengthIndex
from nautilus_trader.model import BarType, InstrumentId, OrderSide, Quantity

from bagel.strategies.base import BaseStrategy
from bagel.universe import daily_bar_type


class RsiReversion(BaseStrategy):
    def __init__(
        self,
        instrument_id: InstrumentId,
        bar_type: BarType | None = None,
        period: int = 14,
        lower: float = 30.0,
        upper: float = 70.0,
        trade_size: int = 100,
    ) -> None:
        super().__init__()
        self._instrument_id = instrument_id
        self._bar_type = bar_type or daily_bar_type(instrument_id)
        self._qty = Quantity.from_int(trade_size)
        self._rsi = RelativeStrengthIndex(period)
        self._lower = lower
        self._upper = upper

    def on_start(self) -> None:
        self.register_indicator_for_bars(self._bar_type, self._rsi)
        self.subscribe_bars(self._bar_type)

    def on_bar(self, bar) -> None:
        if not self._rsi.initialized:
            return
        is_flat = self.portfolio.is_net_flat(self._instrument_id)
        if self._rsi.value < self._lower and is_flat:
            self.submit_order(
                self.order_factory.market(self._instrument_id, OrderSide.BUY, self._qty)
            )
        elif self._rsi.value > self._upper and not is_flat:
            self.close_all_positions(self._instrument_id)
