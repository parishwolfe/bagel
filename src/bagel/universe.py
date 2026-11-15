"""The tradable universe for the lab.

Every symbol resolves on IB's ``ARCA`` venue (empirically verified against the
paper Gateway — all eight return consolidated 1-minute trade bars there). We
standardise on 1-minute bars as the *base* granularity and rebuild higher
timeframes with Nautilus composite bar types (see :func:`composite_bar_type`).
"""

from __future__ import annotations

from dataclasses import dataclass

from nautilus_trader.model import BarType, InstrumentId

VENUE = "ARCA"


@dataclass(frozen=True)
class Etf:
    symbol: str
    description: str
    role: str

    @property
    def instrument_id(self) -> InstrumentId:
        return InstrumentId.from_str(f"{self.symbol}.{VENUE}")


UNIVERSE: tuple[Etf, ...] = (
    Etf("SPY", "S&P 500", "core large-cap"),
    Etf("QQQ", "Nasdaq-100", "tech / growth"),
    Etf("IWM", "Russell 2000", "small caps"),
    Etf("DIA", "Dow 30", "blue chip"),
    Etf("TLT", "20+yr US Treasuries", "bonds / risk-off"),
    Etf("GLD", "Gold", "commodity / hedge"),
    Etf("UPRO", "3x S&P 500 (leveraged)", "leveraged long"),
    Etf("TQQQ", "3x Nasdaq-100 (leveraged)", "leveraged long"),
)

SYMBOLS: tuple[str, ...] = tuple(e.symbol for e in UNIVERSE)


def instrument_ids(symbols: list[str] | None = None) -> list[InstrumentId]:
    """InstrumentIds for the given symbols (default: the whole universe)."""
    wanted = set(symbols) if symbols else None
    return [e.instrument_id for e in UNIVERSE if wanted is None or e.symbol in wanted]


def base_bar_type(instrument_id: InstrumentId) -> BarType:
    """The stored 1-minute base series (``EXTERNAL`` = sourced from IB)."""
    return BarType.from_str(f"{instrument_id}-1-MINUTE-LAST-EXTERNAL")


def daily_bar_type(instrument_id: InstrumentId) -> BarType:
    """The stored daily series (downloaded directly from IB; deep history)."""
    return BarType.from_str(f"{instrument_id}-1-DAY-LAST-EXTERNAL")


def composite_bar_type(instrument_id: InstrumentId, minutes: int) -> BarType:
    """A higher-timeframe bar aggregated *internally* from the 1-minute base.

    e.g. ``composite_bar_type(SPY.ARCA, 5)`` ->
    ``SPY.ARCA-5-MINUTE-LAST-INTERNAL@1-MINUTE-EXTERNAL``
    Subscribing to this makes Nautilus build 5-minute bars on the fly from the
    1-minute bars already in the catalog — no extra data needed.
    """
    return BarType.from_str(
        f"{instrument_id}-{minutes}-MINUTE-LAST-INTERNAL@1-MINUTE-EXTERNAL"
    )
