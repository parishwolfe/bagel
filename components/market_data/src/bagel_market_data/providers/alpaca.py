"""Alpaca REST pagination and WebSocket protocol adapter.

REST is explicit to expose page boundaries and decode JSON prices as Decimal.
Request models use alpaca-py; transport follows the official API reference.
"""

import asyncio
import json
import logging
import threading
import time
from collections.abc import AsyncIterator, Iterator, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from email.utils import parsedate_to_datetime
from random import uniform
from typing import Any

import httpx
from alpaca.data.enums import DataFeed
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from ..config import Settings
from ..domain import NEW_YORK, Bar, Instrument, MarketClock, Origin, TimeRange, TradingDay

log = logging.getLogger(__name__)


class ProviderError(RuntimeError):
    """Safe to log: never includes headers, response bodies, or credentials."""


class TransientProviderError(ProviderError):
    pass


def normalize_bar(symbol: str, raw: dict[str, Any]) -> Bar:
    timestamp = raw["t"]
    if isinstance(timestamp, str):
        timestamp = datetime.fromisoformat(timestamp)
    return Bar(
        symbol=symbol,
        timestamp=timestamp,
        open=Decimal(str(raw["o"])),
        high=Decimal(str(raw["h"])),
        low=Decimal(str(raw["l"])),
        close=Decimal(str(raw["c"])),
        volume=Decimal(str(raw["v"])),
        vwap=Decimal(str(raw["vw"])) if raw.get("vw") is not None else None,
        trade_count=int(raw["n"]) if raw.get("n") is not None else None,
    )


class AlpacaREST:
    source = "alpaca"

    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self.client = client or httpx.Client(timeout=httpx.Timeout(10, connect=10))
        self._owns_client = client is None
        self._rate_lock = threading.Lock()
        self._last_request = 0.0
        self._stop = threading.Event()
        self.symbol_asof = datetime.now(NEW_YORK).date().isoformat()

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def cancel(self) -> None:
        """Wake throttling/retry waits when the worker receives a shutdown signal."""
        self._stop.set()

    def _pause(self, seconds: float) -> None:
        if self._stop.wait(seconds):
            raise TransientProviderError("Provider operation cancelled")

    def _get(self, url: str, params: dict[str, Any]) -> dict[str, Any] | list[Any]:
        key, secret = self.settings.alpaca_api_key, self.settings.alpaca_secret_key
        if key is None or secret is None:
            raise ProviderError("Missing Alpaca API key or secret")
        headers = {
            "APCA-API-KEY-ID": key.get_secret_value(),
            "APCA-API-SECRET-KEY": secret.get_secret_value(),
        }
        for attempt in range(6):
            if self._stop.is_set():
                raise TransientProviderError("Provider operation cancelled")
            delay = min(30.0, 0.5 * 2**attempt) + uniform(0, 0.25)
            with self._rate_lock:
                self._pause(
                    max(
                        0,
                        60 / self.settings.requests_per_minute
                        - (time.monotonic() - self._last_request),
                    )
                )
                self._last_request = time.monotonic()
            try:
                response = self.client.get(url, params=params, headers=headers)
            except httpx.TransportError:
                response = None
            if response is not None:
                if response.is_success:
                    return json.loads(response.text, parse_float=Decimal)
                if response.status_code not in (408, 429) and response.status_code < 500:
                    raise ProviderError(f"Alpaca request rejected (HTTP {response.status_code})")
                if retry_after := response.headers.get("Retry-After"):
                    try:
                        delay = max(delay, float(retry_after))
                    except ValueError:
                        try:
                            delay = max(
                                delay,
                                (
                                    parsedate_to_datetime(retry_after) - datetime.now(UTC)
                                ).total_seconds(),
                            )
                        except (TypeError, ValueError):
                            pass
            if attempt < 5:
                log.warning("provider_retry", extra={"attempt": attempt + 1})
                if delay > 120:
                    raise TransientProviderError(
                        "Provider requested a retry beyond this retry budget"
                    )
                self._pause(max(delay, 0))
        raise TransientProviderError("Alpaca request exhausted transient retries")

    def instrument(self, symbol: str) -> Instrument:
        raw = self._get(f"{self.settings.alpaca_endpoint}/v2/assets/{symbol}", {})
        if not isinstance(raw, dict) or raw.get("class") != "us_equity":
            raise ProviderError(f"Unsupported instrument: {symbol}")
        if raw.get("status") != "active":
            raise ProviderError(f"Instrument is not active: {symbol}")
        return Instrument(str(raw["id"]), str(raw["symbol"]), str(raw["name"]))

    def calendar(self, start: date, end: date) -> dict[date, TradingDay]:
        raw = self._get(
            f"{self.settings.alpaca_endpoint}/v2/calendar",
            {"start": start.isoformat(), "end": end.isoformat()},
        )
        if not isinstance(raw, list):
            raise ProviderError("Unexpected calendar response")
        days = {}
        for item in raw:
            day = date.fromisoformat(item["date"])

            def local_time(value: str, day: date = day) -> datetime:
                # The calendar endpoint returns NY wall times, including early closes.
                return (
                    datetime.fromisoformat(f"{day}T{value}")
                    .replace(tzinfo=NEW_YORK)
                    .astimezone(UTC)
                )

            days[day] = TradingDay(day, local_time(item["open"]), local_time(item["close"]))
        return days

    def clock(self) -> MarketClock:
        raw = self._get(f"{self.settings.alpaca_endpoint}/v2/clock", {})
        if not isinstance(raw, dict) or not isinstance(raw.get("is_open"), bool):
            raise ProviderError("Unexpected market clock response")
        timestamp = datetime.fromisoformat(str(raw["timestamp"]))
        if timestamp.tzinfo is None:
            raise ProviderError("Market clock timestamp must include timezone")
        return MarketClock(timestamp.astimezone(UTC), raw["is_open"])

    def pages(self, symbols: Sequence[str], window: TimeRange) -> Iterator[list[Bar]]:
        token = None
        seen_tokens: set[str] = set()
        request = StockBarsRequest(
            symbol_or_symbols=list(symbols),
            timeframe=TimeFrame(1, TimeFrameUnit("Min")),
            start=window.start,
            # Alpaca end is inclusive; our public interval is half-open.
            end=window.end - timedelta(microseconds=1),
            feed=DataFeed(self.settings.feed),
            limit=10000,
            asof=self.symbol_asof,
        )
        base_params = request.to_request_fields()
        base_params.update(
            {
                "timeframe": str(request.timeframe),
                "feed": self.settings.feed,
                "adjustment": "raw",
                "currency": "USD",
                "sort": "asc",
            }
        )
        while True:
            params = dict(base_params)
            if token:
                params["page_token"] = token
            raw = self._get("https://data.alpaca.markets/v2/stocks/bars", params)
            if not isinstance(raw, dict) or not isinstance(raw.get("bars"), dict):
                raise ProviderError("Unexpected historical bars response")
            page = []
            for symbol, records in raw["bars"].items():
                if symbol not in symbols:
                    raise ProviderError("Unexpected symbol in historical response")
                for record in records:
                    bar = normalize_bar(symbol, record)
                    if not window.start <= bar.timestamp < window.end:
                        raise ProviderError("Historical response outside requested interval")
                    page.append(bar)
            yield page
            token = raw.get("next_page_token")
            if not token:
                return
            if token in seen_tokens:
                raise ProviderError("Repeated historical pagination token")
            seen_tokens.add(token)


class AlpacaStream:
    """Explicit documented protocol: fail fast on auth and report every disconnect.

    alpaca-py's stream retries some authentication failures internally without
    surfacing them. Keeping transport explicit also lets the worker repair gaps.
    """

    def __init__(self, settings: Settings, on_connection=None, connect_factory=None):
        self.settings = settings
        self.on_connection = on_connection or (lambda connected: None)
        self.connect_factory = connect_factory or connect

    async def events(self, symbols: Sequence[str]) -> AsyncIterator[tuple[Bar, Origin]]:
        key, secret = self.settings.alpaca_api_key, self.settings.alpaca_secret_key
        if key is None or secret is None:
            raise ProviderError("Missing Alpaca API key or secret")

        def decode(frame):
            messages = json.loads(frame, parse_float=Decimal)
            if not isinstance(messages, list):
                raise ProviderError("Unexpected WebSocket response")
            for message in messages:
                if message.get("T") == "error":
                    code = message.get("code")
                    if code in (406, 407, 409):
                        raise TransientProviderError(f"Alpaca stream unavailable (code {code})")
                    raise ProviderError(f"Alpaca stream rejected (code {code})")
            return messages

        try:
            async with self.connect_factory(
                f"wss://stream.data.alpaca.markets/v2/{self.settings.feed}",
                open_timeout=15,
                close_timeout=3,
                ping_interval=20,
                ping_timeout=20,
                max_queue=32,
                max_size=2**20,
            ) as socket:
                welcome = decode(await asyncio.wait_for(socket.recv(), 10))
                if not any(m.get("msg") == "connected" for m in welcome):
                    raise ProviderError("Missing WebSocket greeting")
                await socket.send(
                    json.dumps(
                        {
                            "action": "auth",
                            "key": key.get_secret_value(),
                            "secret": secret.get_secret_value(),
                        }
                    )
                )
                auth = decode(await asyncio.wait_for(socket.recv(), 10))
                if not any(m.get("msg") == "authenticated" for m in auth):
                    raise ProviderError("Missing authentication acknowledgment")
                await socket.send(
                    json.dumps(
                        {"action": "subscribe", "bars": list(symbols), "updatedBars": list(symbols)}
                    )
                )
                subscription = decode(await asyncio.wait_for(socket.recv(), 10))
                if not any(
                    m.get("T") == "subscription"
                    and set(symbols) <= set(m.get("bars", []))
                    and set(symbols) <= set(m.get("updatedBars", []))
                    for m in subscription
                ):
                    raise ProviderError("Missing full subscription acknowledgment")
                self.on_connection(True)
                async for frame in socket:
                    for message in decode(frame):
                        kind = message.get("T")
                        if kind in ("b", "u"):
                            if message.get("S") not in symbols:
                                raise ProviderError("Unexpected stream symbol")
                            yield (
                                normalize_bar(message["S"], message),
                                (Origin.UPDATED if kind == "u" else Origin.LIVE),
                            )
                raise TransientProviderError("Alpaca stream ended")
        except (ConnectionClosed, OSError, TimeoutError) as exc:
            raise TransientProviderError("Alpaca stream disconnected") from exc
        finally:
            self.on_connection(False)
