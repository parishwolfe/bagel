"""Read-only pilot sizing. Includes table/index bytes, excludes WAL/backups/other tables."""

import json
import math

from sqlalchemy import Integer, cast, func, select
from sqlalchemy.orm import Session

from market import models as m
from market.data.cli import errors
from market.data.config import Settings
from market.database import database_engine


def estimate(rows: int, total_bytes: int, symbols: int, years: int, minutes_per_day: int):
    # Deliberately conservative: every calendar day is counted, even holidays/weekends.
    # Actual IEX density is lower; this is a capacity bound, not a volume prediction.
    upper_rows = symbols * years * 366 * minutes_per_day
    bytes_per_row = total_bytes / rows if rows else None
    return {
        "sample_rows": rows,
        "sample_table_and_index_bytes": total_bytes,
        "sample_bytes_per_row": bytes_per_row,
        "watchlist_row_upper_bound": upper_rows,
        "estimated_bar_storage_upper_bound_bytes": (
            math.ceil(upper_rows * bytes_per_row) if bytes_per_row else None
        ),
        "assumptions": "366 full collection days/year; excludes WAL, backups, and other tables",
    }


def main():
    with errors():
        settings = Settings()
        if not settings.database_url:
            raise ValueError("Set DATABASE_URL to the pilot database")
        engine = database_engine(
            settings.database_url.get_secret_value(),
            application_name="bagel-storage-estimate",
            pool_size=1,
            timeout=10,
        )
        try:
            with (
                engine.connect().execution_options(postgresql_readonly=True) as connection,
                Session(bind=connection) as session,
            ):
                rows = session.scalar(select(func.count()).select_from(m.Bar)) or 0
                size = (
                    session.scalar(
                        select(
                            func.coalesce(
                                func.sum(func.pg_total_relation_size(m.PostgresInheritance.inhrelid)), 0
                            )
                        ).where(
                            m.PostgresInheritance.inhparent == cast(func.to_regclass("public.bars"), Integer)
                        )
                    )
                    or 0
                )
        finally:
            engine.dispose()
        opening, closing = settings.window_open, settings.window_close
        minutes = closing.hour * 60 + closing.minute - opening.hour * 60 - opening.minute
        print(
            json.dumps(
                estimate(
                    rows,
                    int(size),
                    len(settings.symbols),
                    settings.max_history_years,
                    minutes,
                ),
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
