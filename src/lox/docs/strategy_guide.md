# Writing a lox strategy

Read this before writing or reviewing `strategy.py`. A complete working example is
`src/lox/examples/orb/strategy.py`.

This project uses NautilusTrader 2.0.0rc9. Its Python API differs from the 1.x
versions most examples online were written for. Everything below was checked
against the installed version; where this guide and your memory disagree, the
guide is right.

## The contract

```python
from __future__ import annotations

from nautilus_trader.indicators import ExponentialMovingAverage

from lox.api import LoxStrategy


class MyVariation(LoxStrategy):
    intraday = True  # False for strategies that hold overnight

    def setup(self) -> None:
        # Called once. Read parameters, build indicators and state.
        self.fast = ExponentialMovingAverage(int(self.params["fast"]))
        self.bars = self.minute_bars(minutes=5)

    def on_start(self) -> None:
        self.register_indicator_for_bars(self.bars, self.fast)
        self.subscribe_bars(self.bars)

    def on_bar(self, bar) -> None:
        if not self.fast.initialized:
            return
        if float(bar.close) > self.fast.value and self.is_flat():
            self.go_long()
        elif float(bar.close) < self.fast.value and not self.is_flat():
            self.flatten()
```

Rules the linter enforces:

- Subclass `LoxStrategy`. Never define `__init__`; build everything in `setup`.
- One class per variation, named exactly as `class` in `spec.json`.
- Imports are limited to `lox.api`, `nautilus_trader.indicators`,
  `nautilus_trader.model`, `numpy`, `pandas`, and the standard modules `math`,
  `statistics`, `datetime`, `zoneinfo`, `collections`, `dataclasses`, `typing`,
  `decimal`, `enum`, `itertools`, `functools`, `bisect`, `heapq`.
- No files, no network, no `open`, `eval`, `exec`, no bare `except:`, no code at
  module level other than imports, classes, functions and constants.

## What the harness gives you

| Attribute | Meaning |
| --- | --- |
| `self.params` | This run's parameter values, keyed by the names in the variation's `grid`. |
| `self.symbols` | Symbols in this run. One in `single` mode, all of them in `basket` mode. |
| `self.symbol` | The first (or only) symbol. Every helper below defaults to it. |
| `self.instrument_id` | Its Nautilus `InstrumentId`. `self.instrument_ids[symbol]` for the others. |

### Bars

- `self.minute_bars(symbol=None, minutes=1)`: bar type for N-minute bars. `minutes=1`
  is the stored series; larger values are built from it automatically.
- `self.daily_bars(symbol=None)`: daily bars. Only when `spec.json` says
  `"timeframe": "daily"`.
- A run has minute bars or daily bars, never both. An intraday strategy that
  needs yesterday's high, low or close keeps track of them from the minute bars.
- A bar has `open`, `high`, `low`, `close`, `volume` (wrap in `float()`),
  `ts_event` (close time, nanoseconds UTC) and `bar_type`.
- Data is regular trading hours only: 390 one-minute bars a day, 09:30 to 16:00
  New York. There are no pre-market bars. A few days a year close at 13:00.

### Time

- `self.session_minute(bar)`: minutes since 09:30 New York at the bar's close.
  The first 1-minute bar is `1`, the bar closing at 10:00 is `30`, the last is `390`.
- `self.session_date(bar)`: the New York date. Compare it with the previous
  bar's date to detect a new session and reset daily state.
- `self.et(bar)`: the close as a New York `datetime`.

Never do time arithmetic on UTC timestamps yourself: daylight saving shifts them.

### Positions and orders

- `self.go_long(symbol=None, fraction=1.0)`: be long that fraction of account
  equity. Covers a short first. Does nothing if already long.
- `self.go_short(symbol=None, fraction=1.0)`: the mirror image.
- `self.flatten(symbol=None)`: cancel working orders and close the position.
  `self.flatten_all()` does every symbol.
- `self.position(symbol=None)`: signed shares held (0 when flat).
  `self.is_flat(symbol=None)`.
- `self.equity()`: account balance in USD. `self.last_price(symbol=None)`: last close seen.
- `self.shares_for(price, fraction=1.0)`: whole shares that fraction of equity buys.

Gross exposure may not exceed 1x equity. Two positions held together (a pair)
use fractions that add up to 1 or less, for example `0.5` each. A run that goes
above the limit is thrown out.

For stop and limit orders use the Nautilus order factory directly:

```python
from nautilus_trader.model import OrderSide

shares = self.shares_for(float(bar.close))
self.go_long()
self.submit_order(
    self.order_factory.stop_market(
        self.instrument_id,
        OrderSide.SELL,
        self.qty(shares),
        self.price(stop_level),
        reduce_only=True,
    )
)
```

`self.qty(shares)` and `self.price(value)` build the `Quantity` and `Price`
those calls need. Other factory methods: `market`, `limit`, `stop_market`,
`trailing_stop_market`, `bracket`.

### What the base class does for you

- **Intraday flattening.** With `intraday = True`, every position is closed and
  every working order cancelled one minute before 16:00, new orders are refused
  after that, and nothing is carried into the next day. Do not write this yourself.
- **Warm-up.** Orders sent before the test window starts are dropped, so
  indicators can warm up on earlier data.
- **Errors.** An exception in any `on_*` method stops the strategy and the run
  is reported as an error with the traceback.

## Fills and look-ahead

A market order sent in `on_bar` fills at that bar's close, one tick against you.
So the decision may use the bar that just closed and anything before it, and
nothing after.

The classic mistake is an indicator that already contains the current bar:

```python
# WRONG: the channel includes this bar's high, so close can never exceed upper
self.register_indicator_for_bars(self.bars, self.channel)
...
if float(bar.close) > self.channel.upper:
    ...

# RIGHT: compare first, then feed the bar in by hand
if self.channel.initialized and float(bar.close) > self.channel.upper:
    ...
self.channel.update_raw(float(bar.high), float(bar.low))
```

Use `register_indicator_for_bars` when the rule means "the indicator including
this bar" (a moving-average cross). Update by hand, after deciding, when the
rule means "the level set by earlier bars" (a breakout of a prior high).

## Built-in indicators

Import from `nautilus_trader.indicators`. Prefer these to your own code. Every
one has `.initialized`, `.reset()`, `handle_bar(bar)` and an `update_raw(...)`
for feeding values by hand.

| Indicator | Constructor | Read | `update_raw` |
| --- | --- | --- | --- |
| `SimpleMovingAverage`, `ExponentialMovingAverage` | `(period)` | `.value` | `(value)` |
| `HullMovingAverage`, `WeightedMovingAverage`, `DoubleExponentialMovingAverage` | `(period)` | `.value` | `(value)` |
| `RelativeStrengthIndex` | `(period)` | `.value` | `(value)` |
| `AverageTrueRange` | `(period)` | `.value` | `(high, low, close)` |
| `BollingerBands` | `(period, k)` | `.upper`, `.middle`, `.lower` | `(value)` |
| `DonchianChannel` | `(period)` | `.upper`, `.middle`, `.lower` | `(high, low)` |
| `KeltnerChannel` | `(period, k_multiplier)` | `.upper`, `.middle`, `.lower` | `(high, low, close)` |
| `MovingAverageConvergenceDivergence` | `(fast_period, slow_period)` | `.value` | `(close)` |
| `Stochastics` | `(period_k, period_d)` | `.value_k`, `.value_d` | `(high, low, close)` |
| `RateOfChange` | `(period)` | `.value` | `(value)` |
| `ZScore` | `(period)` | `.value`, `.mean`, `.std` | `(value)` |
| `LinearRegression` | `(period)` | `.value`, `.slope`, `.r2` | `(value)` |
| `VolumeWeightedAveragePrice` | `()` | `.value` | `(price, volume)` |

Also available: `AdaptiveMovingAverage`, `AroonOscillator`, `CommodityChannelIndex`,
`ChandeMomentumOscillator`, `DirectionalMovement`, `EfficiencyRatio`,
`OnBalanceVolume`, `VolatilityRatio`, `Swings`, and others in the same module.

`VolumeWeightedAveragePrice` accumulates until you call `.reset()`. For a session
VWAP, reset it on the first bar of each day and feed it the typical price:

```python
if self.session_date(bar) != self.day:
    self.day = self.session_date(bar)
    self.vwap.reset()
typical = (float(bar.high) + float(bar.low) + float(bar.close)) / 3
self.vwap.update_raw(typical, float(bar.volume))
```

## Checking your work

```bash
uv run lox lint <slug>
uv run lox backtest <slug> --variant <name>
uv run lox backtest <slug> --variant <name> --params '{"range_minutes": 30}' --window train
```

`backtest` prints JSON. `"status": "ok"` with more than zero `trades` is the
minimum. `"status": "error"` carries the traceback in `"error"`; `"leverage"`
means an order was larger than the account allows; `"timeout"` means `on_bar`
is too slow or loops forever.
