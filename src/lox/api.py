"""The only lox surface a generated strategy may import.

A strategy subclasses :class:`LoxStrategy` and implements ``setup``,
``on_start`` and ``on_bar``. The base class supplies what every strategy would
otherwise get subtly wrong: position sizing against account equity, New York
session time, flattening intraday positions before the close, and a warm-up
period during which no order is sent.

It also guards the run. Nautilus logs an exception raised inside ``on_bar`` and
carries on, which would turn a crashing strategy into a quiet run with made-up
results. Here every ``on_*`` method is wrapped: the first exception halts the
strategy and the harness reports the run as an error.
"""

from __future__ import annotations

import datetime as dt
import functools
import traceback
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from nautilus_trader.model import (
    BarType,
    Currency,
    InstrumentId,
    OrderSide,
    Quantity,
)
from nautilus_trader.trading import Strategy, StrategyConfig

NEW_YORK = ZoneInfo("America/New_York")
SESSION_OPEN_MINUTE = 9 * 60 + 30  # 09:30 New York
SESSION_MINUTES = 390  # 09:30 to 16:00
USD = Currency.from_str("USD")

# Names a generated strategy must not redefine (checked by the linter).
RESERVED = frozenset(
    {
        "submit_order",
        "submit_order_list",
        "et",
        "session_minute",
        "session_date",
        "minute_bars",
        "daily_bars",
        "equity",
        "position",
        "is_flat",
        "last_price",
        "shares_for",
        "qty",
        "price",
        "go_long",
        "go_short",
        "flatten",
        "flatten_all",
    }
)


class Deadline(BaseException):
    """Raised by the harness timer when a run exceeds its time limit."""


@dataclass
class RunContext:
    """What the harness hands a strategy for one backtest."""

    params: dict
    symbols: list[str]
    instrument_ids: dict[str, InstrumentId]
    timeframe: str  # "minute" or "daily"
    capital: float
    max_leverage: float = 1.0
    trade_start_ns: int = 0  # orders before this are dropped (warm-up)
    state: dict = field(default_factory=lambda: {"deadline": False})


def _guarded(method, is_bar: bool):
    """Wrap an ``on_*`` method: run the bar pre-hook, and catch exceptions."""

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        lox = self._lox
        if lox["depth"]:  # a subclass calling super(): already guarded
            return method(self, *args, **kwargs)
        if lox["halted"] or self.ctx.state.get("deadline"):
            return None
        lox["depth"] += 1
        try:
            if is_bar and not self._lox_before_bar(args[0]):
                return None
            return method(self, *args, **kwargs)
        except Deadline:
            self.ctx.state["deadline"] = True
            lox["halted"] = True
        except Exception:
            lox["errors"] += 1
            lox["halted"] = True
            if lox["first_error"] is None:
                lox["first_error"] = traceback.format_exc(limit=6)
        finally:
            lox["depth"] -= 1
        return None

    return wrapper


class LoxStrategy(Strategy):
    """Base class for every generated strategy.

    Class attributes a subclass may set:

    ``intraday`` (default ``True``)
        Positions are closed before the session ends and are never carried
        overnight. Set ``False`` for swing and daily strategies.
    ``flatten_minutes_before_close`` (default ``1``)
        How many minutes before 16:00 New York an intraday strategy is flattened
        and stops accepting new orders.
    """

    intraday: bool = True
    flatten_minutes_before_close: int = 1

    def __init_subclass__(cls, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        for name, member in list(cls.__dict__.items()):
            if name.startswith("on_") and callable(member):
                setattr(cls, name, _guarded(member, is_bar=name == "on_bar"))

    def __new__(cls, *args, **kwargs):
        # The compiled base only accepts its config; absorb our own arguments.
        return super().__new__(cls)

    def __init__(self, ctx: RunContext) -> None:
        super().__init__(StrategyConfig())
        self.ctx = ctx
        self.params = dict(ctx.params)
        self.symbols = list(ctx.symbols)
        self.symbol = self.symbols[0]
        self.instrument_ids = dict(ctx.instrument_ids)
        self.instrument_id = self.instrument_ids[self.symbol]
        self._lox = {
            "depth": 0,
            "errors": 0,
            "first_error": None,
            "halted": False,
            "dropped_orders": 0,
            "day": None,
            "closing": False,
            "ts": 0,
            "last": {},
        }
        try:
            self.setup()
        except Exception:
            self._lox["errors"] += 1
            self._lox["halted"] = True
            self._lox["first_error"] = traceback.format_exc(limit=6)

    # --- to implement -------------------------------------------------------

    def setup(self) -> None:
        """Read ``self.params`` and build indicators. Called once, before the run."""

    # --- time ---------------------------------------------------------------------

    def et(self, bar) -> dt.datetime:
        """New York time of the bar's close."""
        return dt.datetime.fromtimestamp(bar.ts_event / 1e9, NEW_YORK)

    def session_minute(self, bar) -> int:
        """Minutes since 09:30 New York at the bar's close.

        The first 1-minute bar of the day is 1; the bar closing at 16:00 is 390.
        """
        when = self.et(bar)
        return when.hour * 60 + when.minute - SESSION_OPEN_MINUTE

    def session_date(self, bar) -> dt.date:
        return self.et(bar).date()

    # --- data ---------------------------------------------------------------------

    def _iid(self, symbol: str | None) -> InstrumentId:
        return self.instrument_ids[symbol or self.symbol]

    def minute_bars(self, symbol: str | None = None, minutes: int = 1) -> BarType:
        """Bar type for N-minute bars (N > 1 is aggregated from the 1-minute base)."""
        if self.ctx.timeframe != "minute":
            raise ValueError("this run loads daily bars only; use daily_bars()")
        iid = self._iid(symbol)
        if minutes == 1:
            return BarType.from_str(f"{iid}-1-MINUTE-LAST-EXTERNAL")
        return BarType.from_str(
            f"{iid}-{int(minutes)}-MINUTE-LAST-INTERNAL@1-MINUTE-EXTERNAL"
        )

    def daily_bars(self, symbol: str | None = None) -> BarType:
        if self.ctx.timeframe != "daily":
            raise ValueError("this run loads 1-minute bars only; use minute_bars()")
        return BarType.from_str(f"{self._iid(symbol)}-1-DAY-LAST-EXTERNAL")

    # --- account and positions ----------------------------------------------------------

    def equity(self) -> float:
        """Account balance in USD (starting capital plus realised profit and loss)."""
        account = self.portfolio.account(self.instrument_id.venue)
        return float(account.balance_total(USD).as_double())

    def position(self, symbol: str | None = None) -> float:
        """Signed share count: positive long, negative short, 0 flat."""
        return float(self.portfolio.net_position(self._iid(symbol)))

    def is_flat(self, symbol: str | None = None) -> bool:
        return bool(self.portfolio.is_net_flat(self._iid(symbol)))

    def last_price(self, symbol: str | None = None) -> float | None:
        """Close of the most recent bar seen for the symbol."""
        return self._lox["last"].get(self._iid(symbol))

    def shares_for(self, price: float, fraction: float = 1.0) -> int:
        """Whole shares that put ``fraction`` of account equity to work at ``price``."""
        if price <= 0:
            return 0
        fraction = max(0.0, min(float(fraction), 1.0))
        budget = self.equity() * self.ctx.max_leverage * fraction
        return max(int(budget // price), 0)

    def qty(self, shares: int) -> Quantity:
        return Quantity.from_int(int(shares))

    def price(self, value: float, symbol: str | None = None):
        """A ``Price`` rounded to the instrument's tick, for stop and limit orders."""
        return self.cache.instrument(self._iid(symbol)).make_price(float(value))

    # --- orders ----------------------------------------------------------------------

    def _market(self, symbol: str | None, side: OrderSide, fraction: float) -> None:
        price = self.last_price(symbol)
        shares = self.shares_for(price, fraction) if price else 0
        if shares > 0:
            self.submit_order(
                self.order_factory.market(self._iid(symbol), side, self.qty(shares))
            )

    def go_long(self, symbol: str | None = None, fraction: float = 1.0) -> None:
        """Be long ``fraction`` of equity. Covers a short first; no-op if already long."""
        held = self.position(symbol)
        if held > 0:
            return
        if held < 0:
            self.flatten(symbol)
        self._market(symbol, OrderSide.BUY, fraction)

    def go_short(self, symbol: str | None = None, fraction: float = 1.0) -> None:
        """Be short ``fraction`` of equity. Sells a long first; no-op if already short."""
        held = self.position(symbol)
        if held < 0:
            return
        if held > 0:
            self.flatten(symbol)
        self._market(symbol, OrderSide.SELL, fraction)

    def flatten(self, symbol: str | None = None) -> None:
        """Cancel working orders and close the position in one symbol."""
        iid = self._iid(symbol)
        self.cancel_all_orders(iid)
        if not self.portfolio.is_net_flat(iid):
            self.close_all_positions(iid)

    def flatten_all(self) -> None:
        for symbol in self.symbols:
            self.flatten(symbol)

    def _lox_blocked(self) -> bool:
        lox = self._lox
        return (
            lox["halted"]
            or lox["closing"]
            or bool(self.ctx.state.get("deadline"))
            or lox["ts"] < self.ctx.trade_start_ns
        )

    def submit_order(self, order, *args, **kwargs):
        """Send an order, unless the run is warming up, closing out, or halted."""
        if self._lox_blocked():
            self._lox["dropped_orders"] += 1
            return None
        return super().submit_order(order, *args, **kwargs)

    def submit_order_list(self, order_list, *args, **kwargs):
        if self._lox_blocked():
            self._lox["dropped_orders"] += 1
            return None
        return super().submit_order_list(order_list, *args, **kwargs)

    # --- harness hooks -------------------------------------------------------------------

    def _lox_before_bar(self, bar) -> bool:
        lox = self._lox
        lox["ts"] = bar.ts_event
        lox["last"][bar.bar_type.instrument_id] = float(bar.close)
        if self.ctx.timeframe != "minute":
            return True
        minute = self.session_minute(bar)
        if not 1 <= minute <= SESSION_MINUTES:
            return False  # not a regular-hours bar: the strategy never sees it
        if self.intraday:
            day = self.session_date(bar)
            if day != lox["day"]:
                # A new session: nothing intraday survives the night (this also
                # covers early-close days, where the 15:59 rule never fires).
                lox["day"] = day
                lox["closing"] = False
                self.flatten_all()
            cutoff = SESSION_MINUTES - self.flatten_minutes_before_close
            if minute >= cutoff:
                self.flatten_all()
                lox["closing"] = True
        return True
