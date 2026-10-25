"""Read-only pilot sizing. Includes table/index bytes, excludes WAL/backups/other tables."""

import json
import math

import psycopg
from bagel_market_data.cli import errors
from bagel_market_data.config import Settings


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
        with psycopg.connect(
            settings.database_url.get_secret_value(), autocommit=True, connect_timeout=10
        ) as conn:
            conn.execute("set default_transaction_read_only=on")
            rows = conn.execute("select count(*) from market_data.bars").fetchone()
            size = conn.execute(
                "select coalesce(sum(pg_total_relation_size(inhrelid)),0) "
                "from pg_inherits where inhparent='market_data.bars'::regclass"
            ).fetchone()
            assert rows and size
        opening, closing = settings.window_open, settings.window_close
        minutes = closing.hour * 60 + closing.minute - opening.hour * 60 - opening.minute
        print(
            json.dumps(
                estimate(
                    rows[0],
                    int(size[0]),
                    len(settings.symbols),
                    settings.max_history_years,
                    minutes,
                ),
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
