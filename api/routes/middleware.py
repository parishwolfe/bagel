from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class NoStoreMiddleware:
    """Keep control-plane responses out of browser and intermediary caches."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def no_store(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message).setdefault("Cache-Control", "no-store")
            await send(message)

        await self.app(scope, receive, no_store)
