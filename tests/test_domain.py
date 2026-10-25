from dataclasses import replace
from datetime import UTC, date, datetime, time
from decimal import Decimal

import pytest
from bagel_market_data.config import Settings
from bagel_market_data.domain import NEW_YORK, TimeRange, TradingDay, parse_boundary, subtract_years
from conftest import make_bar
from pydantic import ValidationError


def test_dates_and_dst():
    assert parse_boundary("2026-01-15").hour == 5
    assert parse_boundary("2026-07-15").hour == 4
    with pytest.raises(ValueError, match="timezone"):
        parse_boundary("2026-01-15T12:00:00")
    assert subtract_years(datetime(2024, 2, 29, tzinfo=UTC), 1).day == 28


def test_chunk_boundaries_are_half_open():
    window = TimeRange(parse_boundary("2026-10-20"), parse_boundary("2026-10-22"))
    chunks = list(window.chunks())
    assert len(chunks) == 3
    assert chunks[0].start == window.start and chunks[-1].end == window.end
    assert all(a.end == b.start for a, b in zip(chunks, chunks[1:], strict=False))


def test_early_close_sessions():
    day = TradingDay(
        date(2026, 11, 27),
        datetime(2026, 11, 27, 9, 30, tzinfo=NEW_YORK),
        datetime(2026, 11, 27, 13, tzinfo=NEW_YORK),
    )

    def classify(hour, minute=0):
        return day.classify(
            datetime(2026, 11, 27, hour, minute, tzinfo=NEW_YORK), time(4), time(20)
        )

    assert classify(3, 59) is None
    assert classify(4) == "pre"
    assert classify(9, 30) == "regular"
    assert classify(13) == "post"
    assert classify(20) is None


@pytest.mark.parametrize(
    "change",
    [
        {"high": Decimal("99")},
        {"volume": Decimal("-1")},
        {"open": Decimal("NaN")},
        {"vwap": Decimal("Infinity")},
        {"trade_count": -1},
        {"timestamp": datetime(2026, 1, 1)},
    ],
)
def test_invalid_bars_rejected(change):
    with pytest.raises(ValueError):
        replace(make_bar(), **change)


def test_history_cap_and_future_end(settings):
    with pytest.raises(ValueError, match="MAX_HISTORY"):
        settings.history_window("2016-01-01", "2026-01-01")
    with pytest.raises(ValueError, match="eligible"):
        settings.history_window("2098-01-01", "2099-01-01")
    window = settings.history_window()
    assert window.start == subtract_years(window.end, 5)


def test_symbols_and_secret_diagnostics():
    settings = Settings(_env_file=None, BAGEL_SYMBOLS="spy, qqq, SPY")
    assert settings.symbols == ["SPY", "QQQ"]
    assert settings.missing_credentials() == ["ALPACA_API_KEY", "ALPACA_SECRET_KEY", "DATABASE_URL"]
    with pytest.raises(ValidationError):
        Settings(_env_file=None, BAGEL_SYMBOLS=",")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, BAGEL_WINDOW_OPEN="20:00", BAGEL_WINDOW_CLOSE="04:00")


def test_basic_symbol_limit():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, BAGEL_SYMBOLS=[f"S{i}" for i in range(31)])


def test_separate_supabase_fields_support_unescaped_passwords():
    from psycopg.conninfo import conninfo_to_dict

    password = "example @ password's slash\\"
    settings = Settings(
        _env_file=None,
        SUPABASE_HOST="aws-0-example.pooler.supabase.com",
        SUPABASE_USER="postgres.example",
        SUPABASE_PASSWORD=password,
    )
    assert settings.database_url is not None
    parsed = conninfo_to_dict(settings.database_url.get_secret_value())
    assert parsed["password"] == password and parsed["sslmode"] == "require"
    assert password not in repr(settings)
