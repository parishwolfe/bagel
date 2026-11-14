"""Import an already-downloaded, checksummed Massive daily minute file locally."""

import argparse
import json
from datetime import UTC, datetime, time
from pathlib import Path

from market.data.collectors import MASSIVE_STOCK_FEED
from market.data.domain import NEW_YORK, Observation, Origin, TimeRange, parse_boundary
from market.data.providers.massive import stock_calendar
from market.data.providers.massive_files import FileCatalogue, read_minutes, verify_file
from market.data.storage import PostgresStore
from market.data.watchlist import CapacityExceeded
from market.database import database_session, require_local_database
from market.events.commands import append_command
from market.events.config import EventSettings
from market.research.artifacts import digest_json


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path)
    parser.add_argument("--catalogue", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--batch-rows", type=int, default=500)
    parser.add_argument("--row-budget", type=int, default=100_000_000)
    args = parser.parse_args()
    if not 1 <= args.batch_rows <= 10000 or args.row_budget < 1:
        parser.error("Invalid explicit batch or row resource budget")
    settings = EventSettings()
    dsn = settings.dsn()
    require_local_database(dsn)
    window = TimeRange(parse_boundary(args.start), parse_boundary(args.end))
    if window.end > datetime.now(UTC):
        raise ValueError("Import window must contain completed historical minutes")
    if args.catalogue.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("Catalogue exceeds the metadata resource budget")
    catalogue = FileCatalogue.model_validate_json(args.catalogue.read_bytes())
    verify_file(args.file, args.sha256)
    # Calendar lookup is local exchange-calendars data, not a provider request.
    calendar = stock_calendar(window.start.astimezone(NEW_YORK).date(), window.end.astimezone(NEW_YORK).date())
    observed = datetime.now(UTC)
    count = 0
    with PostgresStore(dsn) as store:

        def write_batch(observations: list[Observation]) -> None:
            if store.database_bytes() >= settings.database_budget_mb * 1_000_000:
                raise CapacityExceeded("Flat-file import reached the configured database size budget")
            store.write_live(series, observations)

        series = store.resolve_series(catalogue.selected(), MASSIVE_STOCK_FEED, "massive")
        identity = store.start_run("backfill", list(catalogue.instruments), window)
        batch = []
        try:
            for bar in read_minutes(args.file, catalogue, window, args.row_budget):
                local = bar.timestamp.astimezone(NEW_YORK)
                day = calendar.get(local.date())
                if day is None or not time(4) <= local.time() < time(20):
                    continue
                session = (
                    "pre" if bar.timestamp < day.open else "regular" if bar.timestamp < day.close else "post"
                )
                batch.append(Observation(bar, session, Origin.HISTORICAL, observed, observed))
                if len(batch) == args.batch_rows:
                    write_batch(batch)
                    count += len(batch)
                    batch.clear()
            if batch:
                write_batch(batch)
                count += len(batch)
            if not count:
                raise ValueError("No selected stock minutes matched the import window")
            verify_file(args.file, args.sha256)
            with database_session(store.engine) as conn:
                append_command(
                    conn,
                    identity,
                    "audit.data.flat_file_import",
                    "massive",
                    {
                        "run_id": str(identity),
                        "source_sha256": args.sha256,
                        "catalogue_sha256": digest_json(catalogue.model_dump(mode="json")),
                        "rows": count,
                        "feed": MASSIVE_STOCK_FEED,
                        "availability": "revised_history",
                    },
                )
            store.finish_run(identity)
        except Exception as error:
            store.finish_run(identity, type(error).__name__)
            raise
    print(
        json.dumps(
            {"run_id": str(identity), "rows": count, "provider_calls": 0, "availability": "revised_history"}
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"error_type": type(error).__name__}))
        raise SystemExit(1) from None
