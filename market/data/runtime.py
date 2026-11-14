"""CLI/embedded adapter for the same Redis-first live collector used by Compose."""

import asyncio
import signal
from threading import Event

from market.data.config import Settings
from market.data.live import run_live


async def run_worker(
    settings: Settings,
    store=None,
    provider=None,
    reload_settings=None,
    *,
    stop=None,
    health=None,
    metrics=None,
) -> bool:
    """Historical persistence is a separate worker; no live database writer exists here."""
    stopping = Event()
    if stop is None:
        loop = asyncio.get_running_loop()
        for name in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(name, stopping.set)

    async def observe_stop() -> None:
        if stop is not None:
            await stop.wait()
            stopping.set()
        else:
            await asyncio.to_thread(stopping.wait)

    watcher = asyncio.create_task(observe_stop())
    try:
        await run_live(settings, stopping, provider=provider, health=health, metrics=metrics)
    finally:
        stopping.set()
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
    return False
