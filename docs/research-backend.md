# Offline research backend

Research, data ingestion, strategy decisions, risk checks, simulation, and
portfolio accounting are modules of the same `market/` backend. One `api`
container, named `bagel-api`, runs FastAPI and its research background worker in one process, with
a persistent volume for Parquet datasets and result artifacts. Local PostgreSQL holds
manifests, job state, and summaries.

Start the entire stack with `docker compose up` using the
[local startup instructions](../README.md#local-postgresql-stack). The API lifecycle
owns optional ingestion, gated by `BAGEL_INGESTION_ENABLED=false` in Compose.
Keep it disabled while the provider access hold is in effect. `/health/ready`
reports both background modules, and `/metrics` serves ingestion metrics on the
same API port. No separate worker or tools container is needed.

## Run a synthetic experiment

Generate a fixture and use its `dataset_id` in a batch definition.

```sh
docker compose exec -T api bagel-research fixture --sessions 3
```

```json
{
  "dataset_id": "<dataset_id>",
  "runs": [
    {"strategy": {"name": "buy_and_hold", "target_weight": "0.10"}},
    {
      "strategy": {"name": "sma_cross", "fast": 5, "slow": 20, "short_below": true},
      "borrow_instruments": ["fixture-alpha", "fixture-beta"],
      "borrow_annual_bps": "500"
    }
  ]
}
```

Save the definition as `batch.json`, submit it, and use the returned batch UUID
to read status and summaries. A running worker claims new jobs automatically.

```sh
docker compose exec -T api bagel-research submit /dev/stdin < batch.json
docker compose exec -T api bagel-research batch <batch-id>
```

A batch accepts up to 1,000 explicit variants. Identical specs, dataset IDs, and
engine fingerprints share runs. A job whose background worker loses its lease becomes
claimable again within the attempt budget. Canceled and expired claims cannot
publish results. The API and worker share the same installed code and engine hash.
Results include summary metrics, daily equity, and fill details. PostgreSQL
owns state transitions and workers claim rows with `FOR UPDATE SKIP LOCKED`.
[PostgreSQL locking clauses](https://www.postgresql.org/docs/17/sql-select.html#SQL-FOR-UPDATE-SHARE).

## Research API

`http://127.0.0.1:8001/docs` lists the research routes. This local-only API has no
authentication, API keys, or operator allowlist. Compose publishes it only on
loopback. Runs submitted locally have no authenticated actor. Do not expose it
publicly; production authentication is deferred and requires a separate design.
The local connection uses the configured database superuser by explicit choice.
Application tables live in `public`, with distinct `ingestion_runs` and
`research_runs` names. Backend roles, grants and RLS remain in SQL for hosted use, but
the local superuser bypasses RLS. Both pools are SQLAlchemy-managed with
`pool_pre_ping=True`. [FastAPI lifespan](https://fastapi.tiangolo.com/advanced/events/).

The API lists strategies and datasets, submits batches, returns run and attempt
status, cancels or retries runs, and serves completed result artifacts. The CLI
is available directly from the API container for development, including
`docker compose exec api bagel-market-data --help` for ingestion CLI usage.

## Use imported market data

Once history has been imported into local PostgreSQL, select one raw USD minute
series per instrument. Use a bounded half-open window and repeat `--series` for
each series ID.

```sh
docker compose exec -T api bagel-research export \
  --series 1 --series 2 --start 2026-10-01 --end 2026-10-31
```

Export reads regular-session rows from one repeatable-read database snapshot.
It writes a checksum-addressed Parquet file and records provider/feed and
canonical instrument identity. Imported revised history is labeled separately
from synthetic data. An optional `--actions` JSON file pins supplied split
and cash-distribution events. [PyArrow Parquet writer](https://arrow.apache.org/docs/python/generated/pyarrow.parquet.ParquetWriter.html).

The first simulator uses whole-share, one-minute bar replay and adverse
slippage/commission assumptions. Signal bars determine order quantity and known
liquidity. Fills use an eligible future open. Shorts need an explicit eligibility and fee
scenario. Effective corporate-action timestamps come from the supplied input.
Historical borrow observations, a complete corporate-action feed, automated
walk-forward analysis, promotion, and broker routing are later work. Synthetic
runs validate the machinery and carry no evidence about market performance.
