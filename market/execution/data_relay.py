"""Narrow credential relay: collectors cannot access broker mutations or keys.

Alpaca's Trading API key authenticates BOTH data and order endpoints. With the
user's single key pair, sharing it with a collector would violate execution's
credential boundary. This internal adapter authenticates data transport only;
the collector still owns subscriptions, reconnects, normalization and buffering.
"""

import asyncio
import hmac
import json
import re
from threading import Event

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from market.execution.alpaca import PAPER_REST


class RelaySettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", populate_by_name=True, hide_input_in_errors=True)
    api_key: SecretStr | None = Field(default=None, validation_alias="ALPACA_API_KEY", repr=False)
    secret_key: SecretStr | None = Field(default=None, validation_alias="ALPACA_SECRET_KEY", repr=False)
    service_key: SecretStr | None = Field(default=None, validation_alias="BAGEL_API_KEY", repr=False)

    def credentials_present(self) -> bool:
        return bool(
            self.api_key
            and self.api_key.get_secret_value().strip()
            and self.secret_key
            and self.secret_key.get_secret_value().strip()
        )


def relay_app(settings: RelaySettings) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health/live")
    def live() -> dict:
        return {"ok": True}

    def authorize(value: str | None) -> None:
        if (
            not settings.service_key
            or not value
            or not hmac.compare_digest(settings.service_key.get_secret_value().encode(), value.encode())
        ):
            raise HTTPException(401, "Service authentication required")
        if not settings.credentials_present():
            raise HTTPException(503, "Alpaca integration is disabled")

    def headers() -> dict[str, str]:
        assert settings.api_key and settings.secret_key
        return {
            "APCA-API-KEY-ID": settings.api_key.get_secret_value(),
            "APCA-API-SECRET-KEY": settings.secret_key.get_secret_value(),
        }

    @app.get("/{service}/{path:path}")
    async def read(service: str, path: str, request: Request) -> Response:
        authorize(request.headers.get("x-api-key"))
        if service == "data" and path == "v2/stocks/bars":
            base = "https://data.alpaca.markets"
        elif service == "reference" and (
            path in ("v2/assets", "v2/calendar", "v2/clock")
            or re.fullmatch(r"v2/assets/[A-Z][A-Z0-9.\-]{0,19}", path)
        ):
            base = PAPER_REST
        else:
            raise HTTPException(404, "Read capability not available")
        try:
            async with httpx.AsyncClient(timeout=15, trust_env=False, follow_redirects=False) as client:
                async with client.stream(
                    "GET", f"{base}/{path}", params=httpx.QueryParams(request.query_params), headers=headers()
                ) as response:
                    chunks = bytearray()
                    async for chunk in response.aiter_bytes():
                        chunks.extend(chunk)
                        if len(chunks) > 32 * 1024 * 1024:
                            raise HTTPException(502, "Provider response exceeds bounded transport size")
                    if not response.is_success:
                        # Provider rejection bodies aren't needed by collectors and
                        # may contain sensitive diagnostics. Preserve only status.
                        return Response(status_code=response.status_code)
                    return Response(bytes(chunks), media_type="application/json")
        except httpx.TransportError:
            raise HTTPException(503, "Data transport unavailable") from None

    @app.websocket("/stream/{feed}")
    async def stream(socket: WebSocket, feed: str) -> None:
        try:
            authorize(socket.headers.get("x-api-key"))
        except HTTPException:
            await socket.close(code=1008)
            return
        if feed not in ("iex", "sip"):
            await socket.close(code=1008)
            return
        await socket.accept()
        try:
            async with connect(
                f"wss://stream.data.alpaca.markets/v2/{feed}",
                open_timeout=10,
                close_timeout=3,
                ping_interval=20,
                ping_timeout=20,
                max_queue=16,
                max_size=2**20,
            ) as upstream:
                greeting = await asyncio.wait_for(upstream.recv(), 10)
                await socket.send_text(greeting.decode() if isinstance(greeting, bytes) else greeting)
                # Preserve the adapter's handshake but consume only a local auth
                # control message; provider credentials never leave this process.
                auth = await asyncio.wait_for(socket.receive_json(), 10)
                if auth.get("action") != "auth":
                    await socket.close(code=1008)
                    return
                assert settings.api_key and settings.secret_key
                await upstream.send(
                    json.dumps(
                        {
                            "action": "auth",
                            "key": settings.api_key.get_secret_value(),
                            "secret": settings.secret_key.get_secret_value(),
                        }
                    )
                )
                authenticated = await asyncio.wait_for(upstream.recv(), 10)
                await socket.send_text(
                    authenticated.decode() if isinstance(authenticated, bytes) else authenticated
                )

                async def controls() -> None:
                    while True:
                        message = await socket.receive_json()
                        if message.get("action") not in ("subscribe", "unsubscribe") or set(message) - {
                            "action",
                            "bars",
                            "updatedBars",
                        }:
                            await socket.close(code=1008)
                            return
                        await upstream.send(json.dumps(message))

                async def events() -> None:
                    async for frame in upstream:
                        await socket.send_text(frame.decode() if isinstance(frame, bytes) else frame)

                tasks = [asyncio.create_task(controls()), asyncio.create_task(events())]
                try:
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        except (ConnectionClosed, WebSocketDisconnect, OSError, TimeoutError, ValueError):
            try:
                await socket.close(code=1011)
            except RuntimeError:
                pass  # The peer already completed the close handshake.

    return app


async def serve_relay(settings: RelaySettings, stop: Event) -> None:
    server = uvicorn.Server(
        uvicorn.Config(
            relay_app(settings),
            host="0.0.0.0",
            port=8011,
            access_log=False,
            log_config=None,
            log_level="warning",
        )
    )

    async def shutdown() -> None:
        await asyncio.to_thread(stop.wait)
        server.should_exit = True

    watcher = asyncio.create_task(shutdown())
    try:
        await server.serve()
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
