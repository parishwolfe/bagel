"""Apply reviewed SQL files transactionally, with checksums and a migration lock.

Defaults to refusing remote database hosts. No credentials are accepted on the CLI.
This runner owns the bagel_meta history; do not mix it with supabase db push.
"""

import argparse
import hashlib
import os
from pathlib import Path
from typing import LiteralString, cast

import psycopg
from dotenv import load_dotenv
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

MIGRATIONS = Path(__file__).resolve().parents[1] / "supabase" / "migrations"


def migrate(dsn: str, *, allow_remote: bool = False, provision_local_role: bool = False) -> int:
    host = conninfo_to_dict(dsn).get("host", "")
    local = host in ("localhost", "127.0.0.1", "::1", "db")
    if not local and not allow_remote:
        raise ValueError("Remote migration refused; hosted rollout requires --allow-remote")
    if provision_local_role and not local:
        raise ValueError("Development role provisioning is local-only")
    applied = 0
    with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
        conn.execute("select pg_advisory_lock(74021800)")
        conn.execute("create schema if not exists bagel_meta")
        conn.execute("revoke all on schema bagel_meta from public")
        conn.execute("""create table if not exists bagel_meta.migrations (
            version text primary key, checksum text not null,
            applied_at timestamptz not null default clock_timestamp()
        )""")
        for path in sorted(MIGRATIONS.glob("*.sql")):
            content = path.read_text()
            checksum = hashlib.sha256(content.encode()).hexdigest()
            previous = conn.execute(
                "select checksum from bagel_meta.migrations where version=%s", (path.name,)
            ).fetchone()
            if previous:
                if previous[0] != checksum:
                    raise ValueError("An applied migration changed; add a new migration instead")
                continue
            with conn.transaction():
                conn.execute(sql.SQL(cast(LiteralString, content)), prepare=False)
                conn.execute(
                    "insert into bagel_meta.migrations(version,checksum) values (%s,%s)",
                    (path.name, checksum),
                )
            applied += 1
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
    parser.add_argument("--allow-remote", action="store_true")
    parser.add_argument("--provision-local-role", action="store_true")
    args = parser.parse_args()
    load_dotenv(override=False)
    dsn = os.environ.get("MIGRATION_DATABASE_URL")
    if not dsn:
        parser.exit(1, "Set MIGRATION_DATABASE_URL to an owner connection string.\n")
    try:
        count = migrate(
            dsn, allow_remote=args.allow_remote, provision_local_role=args.provision_local_role
        )
        print(f"Applied {count} migration(s).")
    except Exception as exc:
        # Never echo driver errors containing connection details.
        parser.exit(
            1, f"Migration failed ({type(exc).__name__}); verify database and migration state.\n"
        )


if __name__ == "__main__":
    main()
