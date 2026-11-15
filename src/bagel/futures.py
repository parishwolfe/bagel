"""ES futures (continuous) instrument + bar-type helpers.

FirstRate ships the E-mini S&P 500 (ES) as a **continuous back-adjusted**
series: the individual quarterly contracts (H/M/U/Z = Mar/Jun/Sep/Dec) are
stitched into one price history with the roll gaps removed, so it behaves like a
single long-lived instrument. We model it as a Nautilus ``FuturesContract`` on
the CME Globex venue (``XCME``) with the real contract economics, so the
backtest accounts for the things that make futures different from the ETFs:

* **multiplier** = ``$50`` per index point (1 tick = 0.25 pt = ``$12.50``) — PnL
  is ``points * 50 * contracts``, not ``points * shares``.
* **price tick** = ``0.25``
* **currency**   = ``USD``
* **venue**      = ``XCME`` (CME Globex), **asset class** ``INDEX``
* **account**    = margin (set up by the backtest runner)

Caveat — back-adjustment: older prices are shifted to remove the roll gaps, so
*returns* are continuous but absolute price *levels* are synthetic (not the
levels that actually traded on those dates). Fine for signal research; do not
build anything that keys off a literal historical price.
"""

from __future__ import annotations

import datetime as dt

from nautilus_trader.model import (
    AssetClass,
    BarType,
    Currency,
    FuturesContract,
    InstrumentId,
    Price,
    Quantity,
    Symbol,
)

FUTURES_VENUE = "XCME"  # CME Globex (ISO 10383 MIC)
ES_SYMBOL = "ES"
ES_INSTRUMENT_ID = InstrumentId.from_str(f"{ES_SYMBOL}.{FUTURES_VENUE}")

ES_MULTIPLIER = 50  # USD per full index point
ES_TICK = 0.25
ES_PRICE_PRECISION = 2

USD = Currency.from_str("USD")

# The span the FirstRate continuous series covers (2008 -> end 2023). The window
# is only used to satisfy the FuturesContract activation/expiration fields; the
# continuous series is synthetic and never actually "expires".
_ACTIVATION = dt.datetime(2008, 1, 1, tzinfo=dt.UTC)
_EXPIRATION = dt.datetime(2025, 1, 1, tzinfo=dt.UTC)

_TIMEFRAME_SPEC = {"1min": "1-MINUTE", "1day": "1-DAY"}


def es_instrument() -> FuturesContract:
    """The continuous ES futures instrument definition (CME, $50/pt, 0.25 tick)."""
    return FuturesContract(
        instrument_id=ES_INSTRUMENT_ID,
        raw_symbol=Symbol(ES_SYMBOL),
        asset_class=AssetClass.INDEX,
        underlying=ES_SYMBOL,
        activation_ns=int(_ACTIVATION.timestamp()) * 1_000_000_000,
        expiration_ns=int(_EXPIRATION.timestamp()) * 1_000_000_000,
        currency=USD,
        price_precision=ES_PRICE_PRECISION,
        price_increment=Price(ES_TICK, ES_PRICE_PRECISION),
        multiplier=Quantity.from_int(ES_MULTIPLIER),
        lot_size=Quantity.from_int(1),
        ts_event=0,
        ts_init=0,
        exchange=FUTURES_VENUE,
    )


def es_bar_type(timeframe: str) -> BarType:
    """Stored ES bar series, e.g. ``es_bar_type("1day")`` -> ``ES.XCME-1-DAY-LAST-EXTERNAL``."""
    if timeframe not in _TIMEFRAME_SPEC:
        raise ValueError(f"timeframe must be one of {list(_TIMEFRAME_SPEC)}")
    return BarType.from_str(
        f"{ES_INSTRUMENT_ID}-{_TIMEFRAME_SPEC[timeframe]}-LAST-EXTERNAL"
    )
