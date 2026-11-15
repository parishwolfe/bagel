"""Donchian channel breakout: buy new highs, exit on new lows.

A *breakout* idea (the classic "Turtle" rule): go long when price closes above
the highest high of the last N bars, exit when it closes below the lowest low.
Defaults to the **daily** series.

The channel is updated **manually at the end of** ``on_bar`` (not via
``register_indicator_for_bars``) so that when we test a breakout we read the
channel built from the *prior* N bars — otherwise the current bar's own high
would already be baked into the upper band and nothing could ever break out.

Concepts introduced:
- a channel indicator (DonchianChannel: upper / middle / lower)
- updating an indicator by hand to control signal timing
"""

from __future__ import annotations

from nautilus_trader.indicators import DonchianChannel
from nautilus_trader.model import BarType, InstrumentId, OrderSide, Quantity

from bagel.strategies.base import BaseStrategy
from bagel.universe import daily_bar_type


class DonchianBreakout(BaseStrategy):
    def __init__(
        self,
        instrument_id: InstrumentId,
        bar_type: BarType | None = None,
        period: int = 20,
        trade_size: int = 100,
    ) -> None:
        super().__init__()
        self._instrument_id = instrument_id
        self._bar_type = bar_type or daily_bar_type(instrument_id)
        self._qty = Quantity.from_int(trade_size)
        self._channel = DonchianChannel(period)

    def on_start(self) -> None:
        # Note: not registered — we update it manually in on_bar (see module docstring).
        self.subscribe_bars(self._bar_type)

    def on_bar(self, bar) -> None:
        # Decide using the channel from the PRIOR bars...
        if self._channel.initialized:
            price = float(bar.close)
            is_flat = self.portfolio.is_net_flat(self._instrument_id)
            if price > self._channel.upper and is_flat:
                self.submit_order(
                    self.order_factory.market(
                        self._instrument_id, OrderSide.BUY, self._qty
                    )
                )
            elif price < self._channel.lower and not is_flat:
                self.close_all_positions(self._instrument_id)

        # ...then fold in the current bar for next time.
        self._channel.update_raw(float(bar.high), float(bar.low))
