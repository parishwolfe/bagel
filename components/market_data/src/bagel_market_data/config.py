"""Environment configuration; secret values never appear in diagnostics."""

import re
from datetime import UTC, datetime, time, timedelta
from typing import Annotated, Literal

from psycopg.conninfo import make_conninfo
from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from .domain import TimeRange, parse_boundary, subtract_years


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_ignore_empty=True, extra="ignore", populate_by_name=True
    )

    symbols: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["SPY", "QQQ", "IWM", "VXUS"],
        validation_alias="BAGEL_SYMBOLS",
    )
    feed: Literal["iex", "sip"] = Field(default="iex", validation_alias="BAGEL_FEED")
    alpaca_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("ALPACA_API_KEY", "APCA_API_KEY_ID")
    )
    alpaca_secret_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "ALPACA_SECRET_KEY", "ALPACA_API_SECRET", "APCA_API_SECRET_KEY"
        ),
    )
    alpaca_endpoint: str = Field(
        default="https://paper-api.alpaca.markets", validation_alias="ALPACA_ENDPOINT"
    )
    database_url: SecretStr | None = Field(default=None, validation_alias="DATABASE_URL")
    supabase_host: str | None = Field(default=None, validation_alias="SUPABASE_HOST", repr=False)
    supabase_port: int = Field(default=5432, validation_alias="SUPABASE_PORT")
    supabase_db: str = Field(default="postgres", validation_alias="SUPABASE_DB")
    supabase_user: str | None = Field(default=None, validation_alias="SUPABASE_USER", repr=False)
    supabase_password: SecretStr | None = Field(default=None, validation_alias="SUPABASE_PASSWORD")
    supabase_sslmode: Literal["require", "verify-ca", "verify-full"] = Field(
        default="require", validation_alias="SUPABASE_SSLMODE"
    )
    history_start: str | None = Field(default=None, validation_alias="BAGEL_HISTORY_START")
    history_end: str | None = Field(default=None, validation_alias="BAGEL_HISTORY_END")
    max_history_years: int = Field(
        default=5, ge=1, le=50, validation_alias="BAGEL_MAX_HISTORY_YEARS"
    )
    auto_backfill: bool = Field(default=True, validation_alias="BAGEL_AUTO_BACKFILL")
    backfill_workers: int = Field(default=4, ge=1, le=4, validation_alias="BAGEL_BACKFILL_WORKERS")
    database_budget_mb: int = Field(default=450, ge=1, validation_alias="BAGEL_DATABASE_BUDGET_MB")
    watchlist_poll_seconds: int = Field(
        default=30, ge=1, validation_alias="BAGEL_WATCHLIST_POLL_SECONDS"
    )
    historical_delay_minutes: int = Field(
        default=15, ge=0, validation_alias="BAGEL_HISTORICAL_DELAY_MINUTES"
    )
    window_open: time = Field(default=time(4), validation_alias="BAGEL_WINDOW_OPEN")
    window_close: time = Field(default=time(20), validation_alias="BAGEL_WINDOW_CLOSE")
    connection_grace_seconds: int = Field(
        default=60, ge=0, le=600, validation_alias="BAGEL_CONNECTION_GRACE_SECONDS"
    )
    correction_grace_seconds: int = Field(
        default=90, ge=30, le=600, validation_alias="BAGEL_CORRECTION_GRACE_SECONDS"
    )
    requests_per_minute: int = Field(
        default=180, ge=1, validation_alias="BAGEL_REQUESTS_PER_MINUTE"
    )
    queue_size: int = Field(default=10000, ge=100, validation_alias="BAGEL_QUEUE_SIZE")
    batch_size: int = Field(default=500, ge=1, le=10000, validation_alias="BAGEL_BATCH_SIZE")
    flush_seconds: float = Field(default=1, gt=0, validation_alias="BAGEL_FLUSH_SECONDS")
    repair_interval_seconds: int = Field(
        default=300, ge=30, validation_alias="BAGEL_REPAIR_INTERVAL_SECONDS"
    )
    reconcile_days: int = Field(default=7, ge=1, le=30, validation_alias="BAGEL_RECONCILE_DAYS")
    reconcile_interval_seconds: int = Field(
        default=21600, ge=300, validation_alias="BAGEL_RECONCILE_INTERVAL_SECONDS"
    )
    health_port: int = Field(default=9090, ge=1024, le=65535, validation_alias="BAGEL_HEALTH_PORT")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(
        default="INFO", validation_alias="BAGEL_LOG_LEVEL"
    )

    @field_validator("symbols", mode="before")
    @classmethod
    def parse_symbols(cls, value):
        symbols = value.split(",") if isinstance(value, str) else value
        result = list(dict.fromkeys(str(s).strip().upper() for s in symbols if str(s).strip()))
        if not result or any(not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,19}", s) for s in result):
            raise ValueError("Use a nonempty comma-separated US equity symbol list")
        return result

    @field_validator("alpaca_endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        value = value.rstrip("/").removesuffix("/v2")
        if value not in ("https://paper-api.alpaca.markets", "https://api.alpaca.markets"):
            raise ValueError("ALPACA_ENDPOINT must be an official Alpaca Trading API endpoint")
        return value

    @model_validator(mode="after")
    def validate_window(self):
        if (
            self.database_url is None
            and self.supabase_host
            and self.supabase_user
            and self.supabase_password
        ):
            self.database_url = SecretStr(
                make_conninfo(
                    host=self.supabase_host,
                    port=self.supabase_port,
                    dbname=self.supabase_db,
                    user=self.supabase_user,
                    password=self.supabase_password.get_secret_value(),
                    sslmode=self.supabase_sslmode,
                )
            )
        if self.window_open >= self.window_close:
            raise ValueError("Collection window must not cross midnight")
        if self.window_open.tzinfo or self.window_close.tzinfo:
            raise ValueError("Collection times are local America/New_York wall times")
        if self.feed == "iex" and len(self.symbols) > 30:
            raise ValueError("IEX Basic configuration is limited to 30 symbols")
        return self

    def missing_credentials(self) -> list[str]:
        return [
            name
            for name, value in (
                ("ALPACA_API_KEY", self.alpaca_api_key),
                ("ALPACA_SECRET_KEY", self.alpaca_secret_key),
                ("DATABASE_URL", self.database_url),
            )
            if value is None or not value.get_secret_value().strip()
        ]

    def require_credentials(self) -> None:
        if missing := self.missing_credentials():
            raise ValueError("Missing configuration: " + ", ".join(missing))

    def eligible_end(self, now: datetime | None = None) -> datetime:
        return (
            (now or datetime.now(UTC)) - timedelta(minutes=self.historical_delay_minutes)
        ).replace(second=0, microsecond=0)

    def history_window(self, start: str | None = None, end: str | None = None) -> TimeRange:
        eligible = self.eligible_end()
        end_text = end or self.history_end
        end_value = parse_boundary(end_text) if end_text else eligible
        start_text = start or self.history_start
        start_value = (
            parse_boundary(start_text)
            if start_text
            else subtract_years(end_value, self.max_history_years)
        )
        window = TimeRange(start_value, end_value)
        if window.start < subtract_years(window.end, self.max_history_years):
            raise ValueError("Requested history exceeds BAGEL_MAX_HISTORY_YEARS")
        if window.end > eligible:
            raise ValueError("History end exceeds the completed, historically eligible minute")
        return window
