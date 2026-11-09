# Offline research backend

Research, data ingestion, strategy decisions, risk checks, simulation, and
portfolio accounting are modules of the same `market/` backend. The API and
research worker use one Docker image and share a volume for Parquet datasets
and result artifacts. Supabase holds manifests, job state, and summaries.

Start the database, apply the local migration, and start `research-api` and
`research-worker` using the [local startup instructions](../README.md#local-supabase-stack).
The ingestion service is opt-in while the provider access hold is in effect.

## Run a synthetic experiment

Generate a fixture and use its `dataset_id` in a batch definition.

```sh
docker compose exec -T research-worker bagel-research fixture --sessions 3
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
docker compose exec -T research-worker bagel-research submit /dev/stdin < batch.json
docker compose exec -T research-worker bagel-research batch <batch-id>
```

A batch accepts up to 1,000 explicit variants. Identical specs, dataset IDs, and
engine fingerprints share runs. A job whose worker loses its lease becomes
claimable again within the attempt budget. Canceled and expired claims cannot
publish results. Keep API and worker on the same image so their engine hashes
match. Results include summary metrics, daily equity, and fill details. PostgreSQL
owns state transitions and workers claim rows with `FOR UPDATE SKIP LOCKED`.
[PostgreSQL locking clauses](https://www.postgresql.org/docs/17/sql-select.html#SQL-FOR-UPDATE-SHARE).

## Research API

`http://127.0.0.1:8001/docs` lists the research routes. To use protected routes,
set `BAGEL_RESEARCH_AUTH_KEY` to the **local** Supabase publishable key and
`BAGEL_RESEARCH_OPERATOR_IDS` to a JSON array of authorized local user UUIDs.
Recreate `research-api` after setting them. Requests carry the user's local
Supabase access token as a bearer token. The API verifies the user with local
Supabase Auth and applies the server-side allowlist. The database tables live in
a private schema with explicit grants and RLS.
[Supabase Auth user verification](https://supabase.com/docs/reference/javascript/auth-getuser),
[Data API security](https://supabase.com/docs/guides/api/securing-your-api),
[FastAPI lifespan](https://fastapi.tiangolo.com/advanced/events/).

The API lists strategies and datasets, submits batches, returns run and attempt
status, cancels or retries runs, and serves completed result artifacts. The CLI
is available directly from the local worker container for development.

## Use imported market data

Once history has been imported into local Supabase, select one raw USD minute
series per instrument. Use a bounded half-open window and repeat `--series` for
each series ID.

```sh
docker compose exec -T research-worker bagel-research export \
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
