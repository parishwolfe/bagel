"""Sample strategies for learning NautilusTrader concepts.

Trend-following
- buy_and_hold      : baseline benchmark (enter once, hold).
- ema_cross         : fast/slow EMA crossover on the native 1-minute bars.
- multi_tf          : EMA cross on a higher timeframe rebuilt from the 1-minute
                      base (demonstrates Nautilus composite bars).
- sma_trend         : single-MA regime filter (classic 200-day), on daily bars.

Mean-reversion
- rsi_reversion     : buy oversold RSI, exit when it recovers, on daily bars.

Breakout
- donchian_breakout : buy N-day highs, exit on N-day lows, on daily bars.
"""

from bagel.strategies.buy_and_hold import BuyAndHold
from bagel.strategies.donchian_breakout import DonchianBreakout
from bagel.strategies.ema_cross import EmaCross
from bagel.strategies.multi_tf import MultiTimeframeEma
from bagel.strategies.rsi_reversion import RsiReversion
from bagel.strategies.sma_trend import SmaTrend

__all__ = [
    "BuyAndHold",
    "DonchianBreakout",
    "EmaCross",
    "MultiTimeframeEma",
    "RsiReversion",
    "SmaTrend",
]
