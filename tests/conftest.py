import os
from datetime import datetime
from decimal import Decimal
from uuid import uuid4

import psycopg
import pytest
from bagel_market_data.config import Settings
from bagel_market_data.domain import Bar
from bagel_market_data.storage import PostgresStore
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from tools.migrate import migrate


@pytest.fixture
def settings():
    return Settings(
        _env_file=None,
        ALPACA_API_KEY="test-key",
        ALPACA_SECRET_KEY="test-secret",
        DATABASE_URL="postgresql://test:test@localhost/test",
        BAGEL_REQUESTS_PER_MINUTE=1000000,
        BAGEL_AUTO_BACKFILL=False,
    )


def make_bar(timestamp="2026-10-22T14:00:00Z", symbol="SPY", close="100"):
    return Bar(
        symbol,
        datetime.fromisoformat(timestamp),
        Decimal("100"),
        Decimal("105"),
        Decimal("95"),
        Decimal(close),
        Decimal("1000"),
        Decimal("100.01"),
        10,
    )


@pytest.fixture(scope="session")
def database():
    dsn = os.environ.get("BAGEL_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Set BAGEL_TEST_DATABASE_URL to a disposable local PostgreSQL admin connection")
    if conninfo_to_dict(dsn).get("host") not in ("localhost", "127.0.0.1", "::1"):
        pytest.fail("Integration tests refuse non-local database hosts")
    name = "bagel_test_" + uuid4().hex[:12]
    admin = make_conninfo(dsn, dbname=name)
    worker = make_conninfo(admin, user="bagel_worker", password="bagel-local-worker")
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
    try:
        assert migrate(admin, provision_local_role=True) == 3
        yield admin, worker
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("drop database {} with (force)").format(sql.Identifier(name)))


@pytest.fixture
def store(database):
    admin, worker = database
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute("""truncate ingestion.watchlists,ingestion.capacity_resets,
          ingestion.ranges,ingestion.runs,ingestion.stream_state,
          market_data.bars,market_data.series,market_data.instrument_mappings,
          market_data.instruments,market_data.sources restart identity cascade""")
    with PostgresStore(worker) as repo:
        yield repo
