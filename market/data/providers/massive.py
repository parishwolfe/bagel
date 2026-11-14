"""Massive stock REST aggregates; never reinterpret indices or derived series as OHLCV."""

import json
import re
import threading
import time
from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import exchange_calendars as calendars
import httpx
from pydantic import Field, SecretStr

from market.data.collectors import MASSIVE_STOCK_FEED, Capabilities
from market.data.domain import Bar, Instrument, TimeRange, TradingDay
from market.data.providers.alpaca import ProviderError, TransientProviderError
from market.events.config import EventSettings

FEED = MASSIVE_STOCK_FEED


def stock_calendar(start: date, end: date) -> dict[date, TradingDay]:
    calendar = calendars.get_calendar(
        "XNYS", start=(start - timedelta(days=7)).isoformat(), end=(end + timedelta(days=7)).isoformat()
    )
    return {
        day.date(): TradingDay(
            day.date(), calendar.session_open(day).to_pydatetime(), calendar.session_close(day).to_pydatetime()
        )
        for day in calendar.sessions_in_range(start, end)
    }


class MassiveSettings(EventSettings):
    api_key: SecretStr | None = Field(default=None, validation_alias="MASSIVE_API_KEY", repr=False)
    requests_per_minute: int = Field(default=5, ge=1, validation_alias="MASSIVE_REQUESTS_PER_MINUTE")


class MassiveREST:
    source = "massive"
    capabilities = Capabilities("massive", False, True, True, True, False)

    def __init__(self, settings: MassiveSettings, client: httpx.Client | None = None):
        self.settings = settings
        self.client = client or httpx.Client(timeout=20, follow_redirects=False, trust_env=False)
        self.owns_client = client is None
        self.stop = threading.Event()
        self.last_request = 0.0

    def cancel(self) -> None:
        self.stop.set()

    def close(self) -> None:
        if self.owns_client:
            self.client.close()

    def _get(self, url: str, params: dict | None = None) -> dict:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "api.massive.com" or parsed.username:
            raise ProviderError("Untrusted Massive pagination origin")
        if self.settings.api_key is None:
            raise ProviderError("Massive integration is disabled: credentials missing")
        # Never put an API key from a next_url into a URL, log or command argument.
        parsed = parsed._replace(
            query=urlencode([(k, v) for k, v in parse_qsl(parsed.query) if k.lower() != "apikey"])
        )
        for attempt in range(3):
            delay = max(0, 60 / self.settings.requests_per_minute - (time.monotonic() - self.last_request))
            if self.stop.wait(delay):
                raise TransientProviderError("Massive operation canceled")
            self.last_request = time.monotonic()
            try:
                response = self.client.get(
                    urlunsplit(parsed),
                    params=params,
                    headers={"Authorization": "Bearer " + self.settings.api_key.get_secret_value()},
                )
            except httpx.TransportError:
                response = None
            if response is not None and response.is_success:
                result = json.loads(response.content, parse_float=Decimal)
                if not isinstance(result, dict):
                    raise ProviderError("Invalid Massive response")
                return result
            if response is not None and response.status_code not in (408, 429) and response.status_code < 500:
                raise ProviderError(f"Massive read rejected (HTTP {response.status_code})")
            if self.stop.wait(min(30, 2**attempt)):
                raise TransientProviderError("Massive operation canceled")
        raise TransientProviderError("Massive exhausted its read retry budget")

    def instrument(self, symbol: str) -> Instrument:
        if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,19}", symbol):
            raise ProviderError("Unsupported stock symbol")
        raw = self._get(f"https://api.massive.com/v3/reference/tickers/{symbol}").get("results", {})
        if raw.get("market") != "stocks" or not raw.get("active"):
            raise ProviderError("Massive series is not an active stock/ETF")
        identity = raw.get("share_class_figi") or raw.get("composite_figi")
        if not identity:
            raise ProviderError("Massive instrument needs an explicit stable identity mapping")
        return Instrument(str(identity), str(raw["ticker"]), str(raw["name"]))

    def calendar(self, start: date, end: date) -> dict[date, TradingDay]:
        return stock_calendar(start, end)

    def pages(self, symbols: Sequence[str], window: TimeRange) -> Iterator[list[Bar]]:
        for symbol in symbols:
            if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,19}", symbol):
                raise ProviderError("Unsupported stock symbol")
            first = int(window.start.timestamp()) * 1000
            last = int(window.end.timestamp()) * 1000 - 1
            url = f"https://api.massive.com/v2/aggs/ticker/{symbol}/range/1/minute/{first}/{last}"
            params = {"adjusted": "false", "sort": "asc", "limit": 50000}
            seen = set()
            while url:
                raw = self._get(url, params)
                if raw.get("adjusted") is not False or raw.get("ticker") != symbol:
                    raise ProviderError("Massive adjustment or stock identity could not be verified")
                if raw.get("status") not in ("OK", "DELAYED"):
                    raise ProviderError("Massive aggregate series is not available")
                page = []
                for item in raw.get("results", []):
                    timestamp = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=int(item["t"]))
                    if window.start <= timestamp < window.end:
                        page.append(
                            Bar(
                                symbol,
                                timestamp,
                                open=Decimal(str(item["o"])),
                                high=Decimal(str(item["h"])),
                                low=Decimal(str(item["l"])),
                                close=Decimal(str(item["c"])),
                                volume=Decimal(str(item["v"])),
                                vwap=Decimal(str(item["vw"])) if item.get("vw") is not None else None,
                                trade_count=int(item["n"]) if item.get("n") is not None else None,
                            )
                        )
                yield page
                next_url = raw.get("next_url")
                if next_url and next_url in seen:
                    raise ProviderError("Repeated Massive pagination URL")
                if next_url:
                    seen.add(next_url)
                    path = urlsplit(next_url).path
                    if not path.startswith(f"/v2/aggs/ticker/{symbol}/range/"):
                        raise ProviderError("Massive pagination changed endpoint capability")
                url = next_url
                params = None
