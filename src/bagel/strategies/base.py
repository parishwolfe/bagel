"""Shared base for the lab's strategies.

``Strategy`` is a compiled (PyO3) class. When you subclass it and pass your own
constructor arguments, Python forwards those args to ``Strategy.__new__``, which
only accepts the config. The ``__new__`` override below absorbs the extra args
so subclasses can take ordinary Python parameters in ``__init__``.

Subclasses set:
  * ``self._bar_type``      — the series the strategy *subscribes* to (may be a
    composite like ``...-5-MINUTE-...-INTERNAL@1-MINUTE-EXTERNAL``).
  * ``self._data_bar_type`` — optional; the *stored* series the backtest must
    load from the catalog. Defaults to ``self._bar_type``; composite strategies
    point this at their 1-minute base instead.
"""

from __future__ import annotations

from nautilus_trader.model import BarType
from nautilus_trader.trading import Strategy, StrategyConfig


class BaseStrategy(Strategy):
    def __new__(cls, *args, **kwargs):
        return super().__new__(cls)

    def __init__(self) -> None:
        super().__init__(StrategyConfig())

    @property
    def bar_type(self) -> BarType:
        """The bar series the strategy subscribes to."""
        return self._bar_type

    @property
    def data_bar_type(self) -> BarType:
        """The stored series the backtest engine must be fed from the catalog."""
        return getattr(self, "_data_bar_type", None) or self._bar_type
