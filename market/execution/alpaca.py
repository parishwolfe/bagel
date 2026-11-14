"""Alpaca Paper transport with explicit, single-attempt mutation semantics.

The SDK's common REST client has a retry loop (including POST). Mutations here
use HTTPX 0.28.1 with transport retries=0 and redirects disabled instead.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from decimal import Decimal
from typing import Any, Literal

import httpx
from pydantic import Field, SecretStr, field_validator
from websockets.asyncio.client import connect

from market.events.config import EventSettings
from market.trading.contracts import BrokerAssetView, BrokerOrderView

PAPER_REST = "https://paper-api.alpaca.markets"
PAPER_STREAM = "wss://paper-api.alpaca.markets/stream"


class BrokerUnavailable(RuntimeError):
    """Safe diagnostics only: never response bodies, URLs with credentials or headers."""


class AmbiguousSubmission(BrokerUnavailable):
    """Dispatch may have reached Alpaca. Retain reservation; NEVER automatically POST again."""


class BrokerRejected(BrokerUnavailable):
    pass


class PaperSettings(EventSettings):
    api_key: SecretStr | None = Field(default=None, validation_alias="ALPACA_API_KEY", repr=False)
    secret_key: SecretStr | None = Field(default=None, validation_alias="ALPACA_SECRET_KEY", repr=False)
    endpoint: str = Field(default=PAPER_REST, validation_alias="ALPACA_ENDPOINT")
    submission_enabled: bool = Field(default=False, validation_alias="BAGEL_PAPER_SUBMISSION_ENABLED")
    market_feed: Literal["iex", "sip"] = Field(default="iex", validation_alias="BAGEL_FEED")
    command_batch: int = Field(default=8, ge=1, le=128, validation_alias="BAGEL_ACCOUNT_COMMAND_BATCH")
    sync_pages: int = Field(default=20, ge=1, le=1000, validation_alias="BAGEL_ACCOUNT_SYNC_PAGES")
    poll_seconds: float = Field(default=5, ge=0.1, le=60, validation_alias="BAGEL_ACCOUNT_POLL_SECONDS")

    @field_validator("endpoint")
    @classmethod
    def paper_only(cls, value: str) -> str:
        if value.rstrip("/").removesuffix("/v2") != PAPER_REST:
            raise ValueError("This adapter supports the official Alpaca Paper endpoint only")
        return PAPER_REST

    def credentials_present(self) -> bool:
        return bool(
            self.api_key
            and self.api_key.get_secret_value().strip()
            and self.secret_key
            and self.secret_key.get_secret_value().strip()
        )


class PaperREST:
    def __init__(self, settings: PaperSettings, transport: httpx.BaseTransport | None = None):
        if not settings.credentials_present():
            raise BrokerUnavailable("Paper credentials are not configured")
        assert settings.api_key is not None and settings.secret_key is not None
        self.settings = settings
        self.client = httpx.Client(
            base_url=PAPER_REST,
            timeout=httpx.Timeout(10, connect=5),
            transport=transport if transport is not None else httpx.HTTPTransport(retries=0),
            follow_redirects=False,
            trust_env=False,
            headers={
                "APCA-API-KEY-ID": settings.api_key.get_secret_value(),
                "APCA-API-SECRET-KEY": settings.secret_key.get_secret_value(),
            },
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        )

    def close(self) -> None:
        self.client.close()

    def get(self, path: str, params: dict | None = None) -> Any:
        if not path.startswith("/v2/") or path.startswith("//"):
            raise ValueError("Invalid broker read path")
        try:
            response = self.client.get(path, params=params)
        except httpx.TransportError:
            raise BrokerUnavailable("Paper read transport failure") from None
        if response.status_code == 404:
            return None
        if not response.is_success:
            raise BrokerUnavailable(f"Paper read rejected (HTTP {response.status_code})")
        try:
            return json.loads(response.content, parse_float=Decimal)
        except (ValueError, UnicodeError):
            raise BrokerUnavailable("Invalid Paper read response") from None

    def submit_once(self, payload: dict) -> dict:
        if not self.settings.submission_enabled:
            raise BrokerRejected("Paper submission is disabled")
        if not payload.get("client_order_id"):
            raise ValueError("A durable client_order_id is required before dispatch")
        try:
            # Deliberately one call, without SDK retry machinery or retry decorators.
            response = self.client.post("/v2/orders", json=payload)
        except httpx.TransportError:
            raise AmbiguousSubmission("Paper submission transport outcome is unknown") from None
        if response.status_code == 422:
            try:
                message = str(response.json().get("message", "")).lower()
            except (ValueError, AttributeError):
                message = ""
            if "client_order_id" in message and ("unique" in message or "duplicate" in message):
                raise AmbiguousSubmission("Client order identity requires broker reconciliation")
        if response.status_code in (400, 401, 403, 422):
            raise BrokerRejected(f"Paper order rejected (HTTP {response.status_code})")
        if not response.is_success:
            raise AmbiguousSubmission(f"Paper submission outcome unknown (HTTP {response.status_code})")
        try:
            result = json.loads(response.content, parse_float=Decimal)
            if not isinstance(result, dict) or not result.get("id"):
                raise ValueError
            order = BrokerOrderView.model_validate(result)
            if (
                order.client_order_id != payload["client_order_id"]
                or order.symbol != payload["symbol"]
                or order.side != payload["side"]
            ):
                raise ValueError
            return result
        except (ValueError, UnicodeError):
            raise AmbiguousSubmission("Paper submission acknowledgment is invalid") from None

    def lookup_client_order(self, identity: str) -> dict | None:
        order = self.get("/v2/orders:by_client_order_id", {"client_order_id": identity})
        if order is not None and order.get("order_class") in ("bracket", "oto", "oco"):
            from uuid import UUID

            return self.get(f"/v2/orders/{UUID(order['id'])}", {"nested": "true"})
        return order

    def asset(self, symbol: str) -> dict:
        raw = self.get("/v2/assets/" + symbol)
        return (
            BrokerAssetView.model_validate(raw).model_dump(mode="json", by_alias=True)
            if raw is not None
            else {}
        )

    def cancel_once(self, broker_order_id: str) -> bool:
        # A known-order cancellation remains available while dispatch is paused.
        from uuid import UUID

        identity = UUID(broker_order_id)
        try:
            response = self.client.delete(f"/v2/orders/{identity}")
        except httpx.TransportError:
            raise BrokerUnavailable("Paper cancellation requires reconciliation") from None
        if response.status_code not in (204, 404, 422):
            raise BrokerUnavailable(
                f"Paper cancellation requires reconciliation (HTTP {response.status_code})"
            )
        return response.status_code == 204

    def fill_pages(self, *, after: str | None = None) -> Iterator[list[dict]]:
        params: dict = {"page_size": 100, "direction": "asc"}
        if after:
            params["after"] = after
        previous = None
        while True:
            rows = self.get("/v2/account/activities/FILL", params)
            if not isinstance(rows, list):
                raise BrokerUnavailable("Invalid fill activity response")
            yield rows
            if len(rows) < 100:
                return
            token = str(rows[-1]["id"])
            if token == previous:
                raise BrokerUnavailable("Repeated fill activity page")
            params["page_token"] = token
            previous = token

    def activity_page(self, cursor: str | None = None, after: str | None = None) -> list[dict]:
        params: dict = {"page_size": 100, "direction": "asc"}
        if cursor:
            params["page_token"] = cursor
        if after:
            params["after"] = after
        rows = self.get("/v2/account/activities", params)
        if not isinstance(rows, list):
            raise BrokerUnavailable("Invalid account activity page")
        return rows

    def order_page(self, status: str = "all", cursor: str | None = None) -> list[dict]:
        params: dict = {"status": status, "limit": 500, "direction": "asc", "nested": "true"}
        if cursor:
            params["after_order_id"] = cursor
        rows = self.get("/v2/orders", params)
        if not isinstance(rows, list):
            raise BrokerUnavailable("Invalid broker order page")
        return rows


async def trading_updates(settings: PaperSettings) -> AsyncIterator[dict]:
    if not settings.credentials_present():
        raise BrokerUnavailable("Paper credentials are not configured")
    assert settings.api_key and settings.secret_key
    async with connect(
        PAPER_STREAM,
        open_timeout=10,
        close_timeout=3,
        ping_interval=20,
        ping_timeout=20,
        max_queue=16,
        max_size=2**20,
    ) as socket:
        await socket.send(
            json.dumps(
                {
                    "action": "auth",
                    "key": settings.api_key.get_secret_value(),
                    "secret": settings.secret_key.get_secret_value(),
                }
            )
        )
        response = json.loads(await asyncio.wait_for(socket.recv(), 10))
        if response.get("stream") != "authorization" or response.get("data", {}).get("status") != "authorized":
            raise BrokerUnavailable("Paper trading stream authorization failed")
        await socket.send(json.dumps({"action": "listen", "data": {"streams": ["trade_updates"]}}))
        response = json.loads(await asyncio.wait_for(socket.recv(), 10))
        if response.get("stream") != "listening" or "trade_updates" not in response.get("data", {}).get(
            "streams", []
        ):
            raise BrokerUnavailable("Paper trading stream subscription failed")
        yield {"kind": "connected"}
        async for frame in socket:
            message = json.loads(frame, parse_float=Decimal)
            if message.get("stream") == "trade_updates":
                yield {"kind": "trade_update", "data": message["data"]}
            elif message.get("action") == "error":
                raise BrokerUnavailable("Paper trading stream error")


OrderSide = Literal["buy", "sell"]
