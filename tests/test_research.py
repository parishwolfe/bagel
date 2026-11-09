from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo
from pydantic import SecretStr

from market.api.research import create_app
from market.backtesting.engine import BacktestSpec, run_backtest
from market.data.domain import MINUTE, Bar, Instrument, Observation, Origin, TimeRange
from market.execution.simulator import FillModel, simulate_open
from market.portfolio.ledger import Fill, Ledger
from market.research.artifacts import Artifacts
from market.research.config import ResearchSettings, require_local_database
from market.research.datasets import export_local, read_bars, synthetic_fixture
from market.research.queue import BatchSpec, Queue
from market.research.worker import work_once
from market.risk.policy import RiskLimits
from market.strategies.core import StrategySpec

START = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)


def bars(prices: list[int], volume: int = 1000) -> list[Bar]:
    return [
        Bar("test", START + i * MINUTE, *(Decimal(price),) * 4, Decimal(volume), None, None)
        for i, price in enumerate(prices)
    ]


def test_orders_use_future_opens_and_only_known_volume():
    price_bars = bars([100, 80, 90, 100, 100])
    at_fill = price_bars[2]
    price_bars[2] = Bar(
        at_fill.symbol,
        at_fill.timestamp,
        at_fill.open,
        at_fill.high,
        at_fill.low,
        at_fill.close,
        Decimal(1),
        None,
        None,
    )
    spec = BacktestSpec(
        initial_cash=10000,
        strategy=StrategySpec(name="buy_and_hold", target_weight=Decimal("0.1")),
        fill_model=FillModel(slippage_bps=0, commission_bps=0, participation=Decimal("0.005")),
    )
    fills = run_backtest(price_bars, spec)["fills"]
    first = fills[0]
    assert first["decision_at"] == START + MINUTE + timedelta(seconds=5)
    assert first["timestamp"] == START + 2 * MINUTE
    assert (first["price"], first["shares"]) == (90, 5)
    assert sum(fill["shares"] for fill in fills) == 10


def test_short_proceeds_distribution_and_cover():
    ledger = Ledger(Decimal(10000), marks={"test": Decimal(100)})
    ledger.book(Fill("test", START, START - MINUTE, -10, Decimal(100), Decimal(0)))
    assert ledger.equity == 10000
    assert ledger.position("test").restricted_proceeds == 1000
    ledger.distribute("test", Decimal(2))
    ledger.charge_borrow(365 * 86400, Decimal(500))
    assert (ledger.distributions, ledger.financing, ledger.equity) == (-20, 50, 9930)
    ledger.marks["test"] = Decimal(90)
    bar = Bar("test", START + MINUTE, *(Decimal(90),) * 4, Decimal(100), None, None)
    fill = simulate_open(
        ledger,
        bar,
        10,
        START,
        Decimal(100),
        FillModel(slippage_bps=0, commission_bps=0, participation=1),
        RiskLimits(),
        False,
    )
    assert isinstance(fill, Fill) and fill.shares == 10
    assert ledger.position("test").shares == 0
    assert (ledger.realized_pnl, ledger.equity) == (100, 10030)


def test_short_scenario_required_and_recorded():
    inputs = bars([100, 99, 98, 97, 96, 95, 94])
    spec = BacktestSpec(
        initial_cash=10000,
        strategy=StrategySpec(fast=1, slow=2, short_below=True),
    )
    rejected = run_backtest(inputs, spec)["summary"]
    assert rejected["fills"] == 0 and rejected["rejections"]["borrow_unavailable"]
    result = run_backtest(inputs, spec.model_copy(update={"borrow_instruments": ("test",)}))
    assert result["summary"]["borrow_is_scenario"]
    assert result["summary"]["borrow_costs"] > 0
    assert result["summary"]["open_positions"]["test"] < 0


def test_fixture_is_repeatable_and_corrupt_artifact_is_rejected(tmp_path):
    artifacts = Artifacts(tmp_path)
    first = synthetic_fixture(artifacts, 1)
    assert first.id == synthetic_fixture(artifacts, 1).id
    assert len(list(read_bars(artifacts, first))) == first.rows == 360
    (artifacts.directory / first.sha256).write_bytes(b"invalid")
    with pytest.raises(ValueError, match="checksum"):
        list(read_bars(artifacts, first))


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://example.com/postgres",
        "host=127.0.0.1 hostaddr=203.0.113.1 dbname=postgres",
        "host=127.0.0.1 service=external",
    ],
)
def test_local_database_rejects_overrides(dsn):
    with pytest.raises(ValueError, match="local database"):
        require_local_database(dsn)


@pytest.fixture
def research_dsn(database):
    return make_conninfo(database[0], user="bagel_research_worker", password="bagel-local-research")


@pytest.mark.integration
def test_worker_claim_fencing_cancel_retry_and_batch_dedup(database, research_dsn, tmp_path):
    artifacts = Artifacts(tmp_path)
    with Queue(research_dsn) as queue:
        dataset_id = queue.register(synthetic_fixture(artifacts, 1))
        spec = BacktestSpec(strategy=StrategySpec(name="buy_and_hold"))
        batch_spec = BatchSpec(dataset_id=dataset_id, runs=(spec, spec))
        batch_id = queue.enqueue(batch_spec)
        assert batch_id == queue.enqueue(batch_spec)
        first = queue.claim()
        assert first is not None
        assert queue.claim() is None
        with psycopg.connect(database[0]) as conn:
            conn.execute(
                "update research.runs set lease_until = clock_timestamp() where id = %s",
                (first.run_id,),
            )
        replacement = queue.claim()
        assert replacement is not None and replacement.run_id == first.run_id
        assert not queue.finish(first, error="stale")
        assert queue.cancel(first.run_id)["state"] == "canceled"
        assert not queue.renew(replacement)
        assert queue.retry(first.run_id)["state"] == "queued"
        assert work_once(queue, artifacts)
        result = queue.run(first.run_id)
        assert result["state"] == "succeeded" and result["summary"]["fills"] == 2
        assert [a["state"] for a in result["attempt_history"]] == [
            "expired",
            "canceled",
            "succeeded",
        ]
        assert artifacts.read(result["result_sha256"]).is_file()
        assert [row["id"] for row in queue.batch(batch_id)["runs"]] == [first.run_id] * 2


@pytest.mark.integration
def test_local_export_read_grants_and_snapshot(store, research_dsn, tmp_path):
    series = store.resolve_series([Instrument("test-security", "TEST", "Test equity")], "iex")
    now = START + MINUTE
    bar = Bar("TEST", START, *(Decimal(100),) * 4, Decimal(100), None, None)
    store.write_live(series, [Observation(bar, "regular", Origin.LIVE, now, now)])
    artifacts = Artifacts(tmp_path)
    manifest = export_local(research_dsn, artifacts, list(series.values()), TimeRange(START, now))
    assert manifest.availability == "revised_history"
    assert next(read_bars(artifacts, manifest)).close == 100
    with psycopg.connect(research_dsn) as connection:
        assert not connection.execute(
            "select has_table_privilege(current_user, 'market_data.bars', 'INSERT')"
        ).fetchone()[0]


@pytest.mark.integration
def test_api_checks_supabase_operator_and_accepts_local_batch(research_dsn, tmp_path):
    operator = uuid4()
    settings = ResearchSettings(
        database_url=SecretStr(research_dsn),
        artifacts=tmp_path,
        auth_key=SecretStr("local-test-key"),
        operator_ids=(operator,),
    )
    app = create_app(settings)

    def authenticate(request):
        assert request.url.path == "/auth/v1/user"
        assert request.headers["apikey"] == "local-test-key"
        token = request.headers["authorization"]
        if token == "Bearer invalid":
            return httpx.Response(401)
        return httpx.Response(
            200, json={"id": str(operator if token == "Bearer valid" else uuid4())}
        )

    with (
        TestClient(app) as client,
        httpx.Client(
            base_url=settings.auth_url, transport=httpx.MockTransport(authenticate)
        ) as auth,
    ):
        app.state.auth = auth
        assert client.get("/health/ready").json()["ok"]
        route = "/v1/research/datasets"
        assert client.get(route).status_code == 401
        assert client.get(route, headers={"Authorization": "Bearer invalid"}).status_code == 401
        assert client.get(route, headers={"Authorization": "Bearer other"}).status_code == 403
        dataset = synthetic_fixture(app.state.artifacts, 2)
        app.state.queue.register(dataset)
        headers = {"Authorization": "Bearer valid"}
        response = client.post(
            "/v1/research/batches",
            headers=headers,
            json={"dataset_id": dataset.id, "runs": [{"strategy": {"name": "cash"}}]},
        )
        assert response.status_code == 202
        detail = client.get(f"/v1/research/batches/{response.json()['batch_id']}", headers=headers)
        assert detail.json()["runs"][0]["actor"] == str(operator)
