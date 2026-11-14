# Bagel

Local, event-driven US stock/ETF research and Alpaca Paper trading. Repository-owned
Python implements strategies, replay, risk, execution and accounting. FastAPI is
the control plane; Next.js is the operator UI. NautilusTrader is an architectural
reference, not the trading engine.

**Local validation only. Production, hosted migrations and broker deployment are
on hold.** [Verification evidence and remaining gates](docs/platform-verification.md).

## Start the local platform

Keep the existing root `.env`. Required runtime configuration is `DB_HOST`,
`DB_PORT`, `DB_DATABASE`, `DB_USER`, `DB_PASSWORD`, and a nonempty `BAGEL_API_KEY`.
There are no fallback credentials. Agents do not create/modify secret environment
files. Compose overrides the internal database host and port; host tools use the
configured host/port. Hosted PostgreSQL remains Supabase.

```sh
docker compose up
```

- UI: <http://127.0.0.1:3000>
- API/OpenAPI: <http://127.0.0.1:8001/docs>
- Unauthenticated probes: `/health/live`, `/health/ready`
- `/v1/*` and `/metrics` require `X-API-Key`. Enter it through the UI or OpenAPI
  **Authorize**, never in a URL or command argument.

The configured superuser/shared-key model is an explicit local-development
decision, not production authentication. API and UI ports are loopback-only; the
database retains the existing Docker Desktop port-binding preference.

| Compose service | Responsibility |
|---|---|
| `db` / `bagel-db` | PostgreSQL 17.11 + TimescaleDB 2.32.0; preserves the existing volume |
| `migrate` | Sole sequential, checksummed forward-migration runner |
| `redis` | Bounded Redis Streams notification transport |
| `api` / `bagel-api` | `uvicorn api.main:app --reload`; orchestration and authenticated controls |
| `market` | Alpaca bars, corrections, reconnect repair and durable history jobs |
| `massive` | Massive REST jobs; visibly disabled without configured credentials |
| `publisher`, `consumer` | Journal publication and recoverable readiness consumption |
| `strategy` | Independently scalable, leased/checkpointable strategy actors |
| `execution` | Account owner, reconciliation, risk, Paper dispatch and credential relay |
| `research` | Bounded experiment generation and leased process-pool trials |
| `ui` | Next.js operator interface; no trading logic/provider credentials |

Migrations apply to fresh **and populated** local volumes before dependent
services start. DDL is not also mounted into `initdb`. Never delete/reset a volume
to migrate. [Database lifecycle and historical DDL](database/README.md).

Source mounts retain API hot reload. Restart Python workers after source changes;
rebuild after dependency changes. Rebuild the UI after frontend changes. Stop with
`docker compose stop`; retain named volumes. Leave the stack running after checks.

## Real data and Paper permission

Only execution receives the existing `ALPACA_ENDPOINT`, `ALPACA_API_KEY`, and
`ALPACA_SECRET_KEY`. An internal authenticated, read-only data relay supplies the
collector. Research and strategy processes receive no broker credentials. This
execution adapter accepts the official **Paper** endpoint only.

Streaming is opt-in. With authorization and configured credentials:

```sh
BAGEL_INGESTION_ENABLED=true docker compose up -d market
```

`SPY,QQQ,IWM,VXUS` is a collector example, **not** a strategy universe. An entitled
full-market subscription can use `BAGEL_SYMBOLS='*'` and `BAGEL_ALPACA_PLAN=plus`;
configuration does not purchase or prove entitlement. Basic IEX is real-time,
with a documented 30-symbol streaming limit and recent-15-minute historical REST
restriction. Feeds never silently switch; untraded minutes are never fabricated.
Current data/replay uses raw USD one-minute stock/ETF bars. Live strategy frames
use regular sessions.

Paper orders require **both** runtime permission and an explicit expiring arm:

```sh
BAGEL_PAPER_SUBMISSION_ENABLED=true docker compose up -d execution
```

This does **not** arm submission. Startup/replacement always pauses the account.
In the UI, inspect readiness, create a trading run UUID, review risk policy, and
explicitly arm it. Inspect the returned durable command. Pause blocks new
submission, not orders already accepted by the broker. Cancel a known broker
order UUID separately. Full reconciliation pauses and requests a fresh history
scan. Restore default permission after an authorized check with
`docker compose up -d execution`.

Retry an uncertain order using its original identity. A timeout/5xx never
authorizes an automatic second POST; a 404 lookup does not prove absence.

## Strategy example

Use **Registered strategies** in the UI or `POST /v1/strategies/runs`:

```json
{
  "name": "local-cross-section-observer",
  "strategy": {
    "name": "cross_section_momentum", "slow": 20, "rank_count": 5,
    "target_weight": "0.10", "short_below": true
  },
  "source": "alpaca", "feed": "iex", "symbols": [], "mode": "signals",
  "missing_policy": "available", "min_coverage_fraction": "0.8"
}
```

Empty `symbols` freezes all currently ingested members in that feed. There is no
fixed strategy list of instruments. State-value/frame-buffer resource budgets
are separate from portfolio exposure policy. New runs are paused. Start through
`/v1/strategies/runs/{id}/start`, inspect `/signals`, and pause through `/pause`.
Pause invalidates undispatched automated intents, but does not liquidate positions.

SMA crossover, buy-and-hold, cash and cross-sectional momentum share the same
checkpointable runtime in live operation and replay. Frames contain contemporaneous
completed bars with an explicit missing-member policy. Late corrections remain
archived without retroactive live trades. Warmup/frame provenance pins exact revisions.

Paper targets additionally require `mode: "paper"`, a verified `account_id`, an
explicitly armed `trading_run_id`, and the account's Alpaca feed. Targets are
strategy-attributed, scaled by `capital_fraction`, then pass through account-wide
locked admission/reservations. Shorts require broker borrow eligibility. There
are no paper-specific symbol/order caps.
`strategy.rebalance_policy` defaults to `on_weight_change`, preserving replay's
fixed whole-share target until its weight changes. Choose `every_frame` explicitly
to re-size each frame. Paper retains sizing per activation and references immutable
signals without copying the full input vector into every command. Broker fills
and Paper equity snapshots are not guaranteed to match simulated execution.

## Historical jobs and reproducible sweeps

The UI can submit/inspect/cancel/retry history jobs, freeze local series, and run
experiments. Freezing never requests provider data. Dataset v2 contains immutable,
checksummed Parquet and an exact-revision sidecar. Revised history is labeled;
it is not presented as original publication history.

Example `POST /v1/research/experiments`, using a frozen dataset ID:

```json
{
  "dataset_id": "replace-with-a-frozen-dataset-id",
  "strategies": [{"name":"sma_cross"}, {"name":"buy_and_hold"}],
  "base": {"split":"exploratory"},
  "grid": {"strategy.fast":[3,5], "strategy.slow":[10,20]},
  "max_pending": 32
}
```

Generation is incremental rather than an in-memory Cartesian-product expansion.
Identical normalized requests reuse identities; invalid combinations remain
visible trials. Results are paginated. Experiment cancellation stops generation,
not shared runs; run cancellation is separate. Claims/completion are token-fenced.

Research never calls providers. It reads local immutable artifacts, simulates
next-eligible-open execution, and records delay, liquidity, fee, slippage and
borrow assumptions. Worker fills stream to checksummed JSONL, not an unbounded
list. Download results/fills in the UI or at `/v1/research/runs/{id}/artifact`
and `/fills`. Identity pins dataset, configuration, code and dependencies.
New runs also archive the selected algorithm sources and dependency metadata;
download them through `/v1/research/engines/{engine_id}` or the run controls.
Archives are evidence, never executable API inputs. Legacy runs predating source
archiving may not have a source archive.
Changed code does not silently execute old queued work: restore its original
engine or submit a new versioned experiment.

Massive jobs use qualified stock aggregates, not an index/derived-series
substitute. `tools/import_massive_minutes.py --help` imports an already-downloaded
stock-minute file, requiring a checksum and stable-identity catalogue. Free-tier
REST verification imported 60 actual SPY minute bars at five requests/minute.
Stocks Basic does not include stock-minute S3 flat files; downloading remains
disabled pending entitlement and complete credentials. No upgrade was attempted.

## Development and capacity

Host development requires Python 3.12, Poetry and Docker Desktop/Compose.

```sh
poetry install
poetry run pyright
pre-commit install --install-hooks
```

Delegate formatting to Ruff hooks; never rewrite applied migrations. New
maintained unit tests require an explicit request. Temporary verification uses
run-owned resources with scoped cleanup.

Offline capacity/recovery runner:

```sh
docker compose run --rm --no-deps \
  --volume ./tools:/app/tools:ro \
  --volume ./database/ddl:/app/database/ddl:ro \
  api python tools/benchmark_platform.py --instruments 4000 --minutes 20 --with-strategies
```

Measured mixed load: about **10,286 ingested bars/sec**, 4,000 instruments, a
10,000-item bounded queue, 1.137s receipt-to-commit p99, and 273 MiB peak runner
RSS. Two strategy actors processed 40 full-universe frames while research
progressed. This is synthetic capacity evidence, **not** proof of full-market
provider entitlement or $25M-AUM reliability.
An 8,000-instrument mixed run measured about 9,367 bars/sec, 1.401s p99 and
403 MiB peak runner RSS. Research used a separate two-instrument dataset; the
subsequent checkpoint-write repeat had no research load and is not a controlled
comparison. Full-market Paper throughput remains unverified.

See [measurements/acceptance gates](docs/platform-verification.md),
[architecture](docs/platform-architecture.md), and [security](docs/security.md).
Local verification does not push/merge/deploy, apply hosted migrations, or alter
the existing hosted automation schedule.
