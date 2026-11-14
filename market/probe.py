"""Container health probe: never prints configuration or exception details."""

import os

from market.data.storage import PostgresStore
from market.events.config import EventSettings
from market.events.journal import Journal


def main() -> None:
    try:
        role = os.environ["BAGEL_WORKER_ROLE"]
        if role in ("market", "strategy", "consumer"):
            from market.events.bus import MarketBus

            bus = MarketBus.from_settings()
            try:
                workers = bus.workers()
            finally:
                bus.close()
        else:
            settings = EventSettings()
            with PostgresStore(settings.dsn()) as store:
                workers = Journal(store.engine).workers()
        ok = any(
            (w["worker"] == role or (role == "strategy" and w["worker"].startswith("strategy:")))
            and w["live"]
            and w["status"] in ("ready", "disabled", "blocked")
            for w in workers
        )
    except Exception:
        ok = False
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
