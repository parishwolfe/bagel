"""Buy-and-hold: the baseline benchmark.

Concepts introduced:
- subscribing to a bar stream (``subscribe_bars``)
- reacting to bars in ``on_bar``
- submitting a market order via ``order_factory``
- reading position state from ``portfolio``

It buys a fixed number of shares on the first bar and then holds forever, so
every other strategy can be judged against simply owning the asset.
"""

from __future__ import annotations

from nautilus_trader.model import BarType, InstrumentId, OrderSide, Quantity

from bagel.strategies.base import BaseStrategy


class BuyAndHold(BaseStrategy):
    def __init__(
        self,
        instrument_id: InstrumentId,
        bar_type: BarType,
        trade_size: int = 100,
    ) -> None:
        super().__init__()
        self._instrument_id = instrument_id
        self._bar_type = bar_type
        self._qty = Quantity.from_int(trade_size)
        self._entered = False

    def on_start(self) -> None:
        self.subscribe_bars(self._bar_type)

    def on_bar(self, bar) -> None:
        if self._entered:
            return
        self.submit_order(
            self.order_factory.market(self._instrument_id, OrderSide.BUY, self._qty)
        )
        self._entered = True
        self.log.info(f"Bought {self._qty} {self._instrument_id} @ {bar.close}")
