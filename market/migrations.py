"""Sole local schema runner. Only immutable migration DDL crosses the SQL boundary.

Ledger reads/writes and catalog inspection use ORM/SQLAlchemy APIs. Schema DDL
cannot be replaced by ORM queries; application modules never use this escape hatch.
"""

import hashlib
import logging
import re
from pathlib import Path

from sqlalchemy import func, inspect, select
from sqlalchemy.orm import Session

from market import models as m
from market.data.observability import configure_logging
from market.database import DatabaseSettings, database_engine, require_local_database

log = logging.getLogger(__name__)
BASELINE_TABLES = {
    "sources",
    "instruments",
    "instrument_mappings",
    "series",
    "bars",
    "ingestion_runs",
    "ranges",
    "stream_state",
    "watchlists",
    "capacity_resets",
    "datasets",
    "research_runs",
    "attempts",
    "batches",
    "batch_runs",
    "migrations",
}


def migrate(dsn: str, directory: Path) -> None:
    require_local_database(dsn)
    files = sorted(directory.glob("[0-9][0-9][0-9][0-9].*.sql"))
    if not files or [int(p.name[:4]) for p in files] != list(range(1, len(files) + 1)):
        raise ValueError("Migration files must be sequential starting at 0001")
    engine = database_engine(dsn, application_name="bagel-migrations", pool_size=1, timeout=10)
    try:
        with engine.connect() as connection, Session(bind=connection) as session:
            session.scalar(select(func.pg_advisory_lock(74021810)))
            session.scalar(select(func.set_config("lock_timeout", "10s", False)))
            session.scalar(select(func.set_config("statement_timeout", "5min", False)))
            m.Base.metadata.tables["public.bagel_schema_migrations"].create(
                session.connection(), checkfirst=True
            )
            session.connection().exec_driver_sql("revoke all on public.bagel_schema_migrations from public")
            session.commit()
            applied = dict(session.execute(select(m.SchemaMigration.name, m.SchemaMigration.sha256)).all())
            catalog = inspect(session.connection())
            if not applied and catalog.has_table("bars", schema="public"):
                tables = set(catalog.get_table_names(schema="public"))
                schemas = set(catalog.get_schema_names())
                columns = {c["name"] for c in catalog.get_columns("research_runs", schema="public")}
                if (
                    not BASELINE_TABLES <= tables
                    or schemas & {"market_data", "ingestion", "research", "bagel_meta"}
                    or not {"lease_token", "engine_id"} <= columns
                ):
                    raise RuntimeError("Unrecognized legacy schema; explicit database review required")
                for path in files[:6]:
                    checksum = hashlib.sha256(path.read_bytes()).hexdigest()
                    session.add(m.SchemaMigration(name=path.name, sha256=checksum, adopted=True))
                    applied[path.name] = checksum
                session.commit()
                log.info("migration_baseline_adopted", extra={"reason": "consolidated_0006"})
            if set(applied) - {p.name for p in files}:
                raise RuntimeError("Database has migrations absent from this checkout")
            session.commit()
            pending_seen = False
            for path in files:
                content = path.read_bytes()
                checksum = hashlib.sha256(content).hexdigest()
                if path.name in applied:
                    if applied[path.name] != checksum or pending_seen:
                        raise RuntimeError("Applied migration checksum or sequence mismatch")
                    continue
                pending_seen = True
                statement = re.sub(r"(?im)^\s*(?:begin|commit);\s*$", "", content.decode())
                with session.begin():
                    # This is checked-in schema DDL, not an application query or
                    # user-supplied executable input. Keep historical bytes intact.
                    session.connection().exec_driver_sql(statement)
                    session.add(m.SchemaMigration(name=path.name, sha256=checksum, adopted=False))
                log.info("migration_applied", extra={"reason": path.name})
            session.scalar(select(func.pg_advisory_unlock(74021810)))
            session.commit()
    finally:
        engine.dispose()


def main() -> None:
    configure_logging("INFO")
    try:
        settings = DatabaseSettings()
        if settings.database_url is None:
            raise ValueError("Database configuration required")
        migrate(settings.database_url.get_secret_value(), Path("/app/database/ddl"))
    except Exception as error:
        log.error("migration_failed", extra={"error_type": type(error).__name__})
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
