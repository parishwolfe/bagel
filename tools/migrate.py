"""Local Compose migrations using Supabase's native applied-version ledger.

Hosted migrations belong to the Supabase GitHub integration. This helper refuses
remote hosts and retains bagel_meta only as a local checksum audit/legacy bridge.
"""

import argparse
import hashlib
import os
import re
from pathlib import Path
from typing import LiteralString, cast

import psycopg
from dotenv import load_dotenv
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

MIGRATIONS = Path(__file__).resolve().parents[1] / "supabase" / "migrations"
# SQL is byte-identical; filenames now match the identities originally assigned
# by Supabase. Adopt a verified old local ledger without replaying any DDL.
LEGACY_FILENAMES = {
    "20261025014047_market_data_foundation.sql": "20261025010646_market_data_foundation.sql",
    "20261025014917_watchlist_capacity_backfills.sql": (
        "20261025014051_watchlist_capacity_backfills.sql"
    ),
    "20261025020628_watchlist_change_trigger.sql": "20261025020433_watchlist_change_trigger.sql",
}


def migrate(dsn: str, *, provision_local_role: bool = False) -> int:
    host = conninfo_to_dict(dsn).get("host", "")
    if host not in ("localhost", "127.0.0.1", "::1", "db"):
        raise ValueError(
            "Remote migration refused; deploy hosted changes through Supabase GitHub CD"
        )
    applied = 0
    with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
        conn.execute("select pg_advisory_lock(74021800)")
        conn.execute("create schema if not exists supabase_migrations")
        conn.execute("revoke all on schema supabase_migrations from public")
        conn.execute("""create table if not exists supabase_migrations.schema_migrations (
            version text primary key, statements text[], name text
        )""")
        conn.execute("create schema if not exists bagel_meta")
        conn.execute("revoke all on schema bagel_meta from public")
        conn.execute("""create table if not exists bagel_meta.migrations (
            version text primary key, checksum text not null,
            applied_at timestamptz not null default clock_timestamp()
        )""")
        for path in sorted(MIGRATIONS.glob("*.sql")):
            match = re.fullmatch(r"(\d{14})_([a-z0-9_]+)\.sql", path.name)
            if not match:
                raise ValueError("Invalid migration filename")
            version, name = match.groups()
            content = path.read_text()
            checksum = hashlib.sha256(content.encode()).hexdigest()
            previous = conn.execute(
                "select checksum from bagel_meta.migrations where version=any(%s)",
                ([path.name, LEGACY_FILENAMES.get(path.name, path.name)],),
            ).fetchall()
            if any(row[0] != checksum for row in previous):
                raise ValueError("An applied migration changed; add a new migration instead")
            native = conn.execute(
                "select name from supabase_migrations.schema_migrations where version=%s",
                (version,),
            ).fetchone()
            if native:
                if native[0] not in (None, name):
                    raise ValueError("Applied migration name does not match the repository")
                continue
            with conn.transaction():
                if not previous:
                    conn.execute(sql.SQL(cast(LiteralString, content)), prepare=False)
                    conn.execute(
                        "insert into bagel_meta.migrations(version,checksum) values (%s,%s)",
                        (path.name, checksum),
                    )
                    applied += 1
                conn.execute(
                    "insert into supabase_migrations.schema_migrations(version,statements,name) "
                    "values (%s,%s,%s)",
                    (version, [content], name),
                )
        if provision_local_role:
            password = os.environ.get("BAGEL_LOCAL_WORKER_PASSWORD", "bagel-local-worker")
            if not conn.execute("select 1 from pg_roles where rolname='bagel_worker'").fetchone():
                conn.execute("create role bagel_worker login in role bagel_ingester")
            conn.execute(
                sql.SQL("alter role bagel_worker password {}").format(sql.Literal(password))
            )
    return applied


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provision-local-role", action="store_true")
    args = parser.parse_args()
    load_dotenv(override=False)
    dsn = os.environ.get("MIGRATION_DATABASE_URL")
    if not dsn:
        parser.exit(1, "Set MIGRATION_DATABASE_URL to a local owner connection string.\n")
    try:
        count = migrate(dsn, provision_local_role=args.provision_local_role)
        print(f"Applied {count} migration(s).")
    except Exception as exc:
        parser.exit(1, f"Migration failed ({type(exc).__name__}); verify local migration state.\n")


if __name__ == "__main__":
    main()
