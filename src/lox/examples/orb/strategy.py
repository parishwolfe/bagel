"""Opening range breakout (ORB): the reference strategy for lox.

Read this before writing a strategy. It shows the whole contract:

* subclass ``LoxStrategy`` and never define ``__init__``;
* ``setup`` reads ``self.params`` and builds state;
* ``on_start`` subscribes to bars;
* ``on_bar`` decides on the bar that just closed, using only that bar and
  earlier ones, and trades through ``go_long`` / ``go_short`` / ``flatten``.

The rule: the first ``range_minutes`` of the session set a high and a low. The
first later close above the high buys; the first close below the low sells
short. The stop is the other side of the range. One trade per day, and the base
class closes whatever is still open one minute before the bell.
"""

from __future__ import annotations

from lox.api import LoxStrategy


class OrbBreakout(LoxStrategy):
    """Trade the first breakout in either direction; stop at the far side of the range."""

    intraday = True
    allow_long = True
    allow_short = True

    def setup(self) -> None:
        self.range_minutes = int(self.params["range_minutes"])
        self.last_entry_minute = int(self.params.get("last_entry_minute", 240))
        self.bars = self.minute_bars()  # the stored 1-minute series
        self.day = None
        self.high = None
        self.low = None
        self.traded = False
        self.stop = None

    def on_start(self) -> None:
        self.subscribe_bars(self.bars)

    def on_bar(self, bar) -> None:
        day = self.session_date(bar)
        if day != self.day:  # first bar of a new session
            self.day, self.high, self.low = day, None, None
            self.traded, self.stop = False, None

        minute = self.session_minute(bar)
        high, low, close = float(bar.high), float(bar.low), float(bar.close)

        if minute <= self.range_minutes:  # still building the opening range
            self.high = high if self.high is None else max(self.high, high)
            self.low = low if self.low is None else min(self.low, low)
            return
        if self.high is None:  # joined the session late: no range, no trade
            return

        held = self.position()
        if held == 0:
            if self.traded or minute > self.last_entry_minute:
                return
            if self.allow_long and close > self.high:
                self.enter(long=True, close=close)
            elif self.allow_short and close < self.low:
                self.enter(long=False, close=close)
        else:
            self.manage(held, close)

    def enter(self, long: bool, close: float) -> None:
        self.traded = True
        if long:
            self.stop = self.low
            self.go_long()
        else:
            self.stop = self.high
            self.go_short()

    def manage(self, held: float, close: float) -> None:
        if (held > 0 and close <= self.stop) or (held < 0 and close >= self.stop):
            self.flatten()


class OrbLongOnly(OrbBreakout):
    """Upside breakouts only."""

    allow_short = False


class OrbTarget(OrbBreakout):
    """Either direction, with a profit target at a multiple of the range height."""

    def setup(self) -> None:
        super().setup()
        self.target_multiple = float(self.params["target_multiple"])
        self.target = None

    def enter(self, long: bool, close: float) -> None:
        height = self.high - self.low
        self.target = close + height * self.target_multiple * (1 if long else -1)
        super().enter(long, close)

    def manage(self, held: float, close: float) -> None:
        hit = (held > 0 and close >= self.target) or (held < 0 and close <= self.target)
        if hit:
            self.flatten()
        else:
            super().manage(held, close)
