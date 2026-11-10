# Bagel

Bagel is a monorepo for an algorithmic trading platform. Root `api/` is the
FastAPI control plane; `market/` contains US equity/ETF ingestion and an offline strategy research workflow.
Research uses frozen datasets and simulated execution. Broker execution and the
operator UI are future milestones.

The initial watchlist is **SPY, QQQ, IWM, VXUS**, using **Alpaca IEX**, raw USD prices,
and a configurable five-year historical range. Collection includes pre/post-market
within 04:00–20:00 America/New_York on trading days. IEX is one exchange: its hours,
liquidity, and available bars differ from consolidated SIP coverage. Empty minutes
are never filled with invented prices.

## Layout

| Location | Responsibility |
|---|---|
| `api/` | FastAPI control plane, global API settings, HTTP contracts, versioned routes, middleware, and `dockerfile` |
| `market/` | Existing data, strategies, backtesting, risk, simulated execution, portfolio accounting, and research modules |
| `ui/` | Reserved for the future Next.js application and its separate deployment |
| `tools/` | Backfill, reconciliation, provisioning, and storage-estimate entry points |
| `database/ddl/` | Sequential PostgreSQL initialization DDL |
| `infra/gcp/` | Terraform with GCS state and Cloud Build deployment configuration |
| `docs/` | Architecture, database history, GCP operations, security, and session record |

## Python setup

Requires Python 3.12, Poetry 2.3.4, and Docker with Compose for local development.

```sh
python3 -m venv .venv
source .venv/bin/activate
poetry install
poetry run bagel-market-data backfill --dry-run
```

Keep your existing `.env`; agents never create or modify secret environment files.
Required additions are supplied in chat for you to apply. Provider ingestion,
when authorized, requires `ALPACA_API_KEY` and `ALPACA_SECRET_KEY`.
`ALPACA_ENDPOINT` is the Trading API endpoint used for assets and calendars;
market data always uses the documented data/stream endpoints. Paper credentials
can authenticate to those market-data endpoints; data entitlements still apply.

Database configuration uses `DB_HOST`, `DB_PORT`, `DB_DATABASE`, `DB_USER`, and
`DB_PASSWORD`. All five are required when using separate fields; passwords need
no manual URL encoding. These fields take precedence over `DATABASE_URL`.
The deployed worker can continue receiving `DATABASE_URL` through secret
injection. Non-local field-based connections require TLS. Use a restricted
backend login belonging to `bagel_ingester` for the deployed worker.
Hosted IPv4 workers should use the **session pooler on port 5432**, with TLS.
Do not use a transaction pooler for the persistent advisory-lock session.

### Git hooks

With `pre-commit` installed, set up the repository's Ruff hooks:

```sh
pre-commit install --install-hooks
pre-commit run --all-files
```

Pre-commit installs the pinned Ruff version in its own isolated environment; no
global Ruff installation, Poetry setup, or running services are needed for the
hooks. They use `ruff.toml` (111-character lines, SQL files excluded) to lint and automatically format
staged Python files on each commit. If formatting changes a file, review and stage
the changes, then retry the commit. Keep the hook revision aligned with Ruff in
`poetry.lock`. The all-files command lints and formats the whole repository on
demand.

## Local PostgreSQL stack

Use Docker Desktop and Compose from the repository root. The single root
`compose.yaml` owns exactly two services: `api` (`bagel-api`) and `db`
(`bagel-db`, standard PostgreSQL 17). No Supabase platform, migration job,
separate CLI stack, or custom infrastructure wrapper is needed. Hosted
PostgreSQL remains Supabase and is never contacted by local research.

Supply the five `DB_*` settings listed above in your existing `.env`.
`DB_HOST=localhost` is for host tools; Compose overrides the API's host to `db`
and its internal port to 5432. The host database port comes from `DB_PORT`.
The database container receives only the values it needs, mapping `DB_USER`,
`DB_PASSWORD`, and `DB_DATABASE` to PostgreSQL's initialization variables.
There are no literal or fallback database credentials in tracked configuration.
PostgreSQL has no default password: `DB_PASSWORD` must be supplied at runtime.

The configured database superuser is used locally by explicit choice. Set a
nonempty `BAGEL_API_KEY` in your existing root `.env`; there is no fallback key.
The API requires it in the `X-API-Key` header on versioned routes and `/metrics`.
Health probes remain unauthenticated. Compose publishes the API only on
`127.0.0.1:8001`. Do not expose this API or deploy its superuser/shared-key configuration.
The existing Docker Desktop database-port binding preference is unchanged.

```sh
docker compose up
```

Compose builds missing backend images. On a fresh database volume, PostgreSQL's
entrypoint executes `database/ddl/NNNN.name.sql` in filename order from the
read-only `/docker-entrypoint-initdb.d/` mount. The API starts after the database
accepts TCP connections. SQL files are **not rerun on restart**, and adding a new
file does not migrate an existing volume. Do not delete volumes to apply a change.
[DDL provenance and lifecycle](database/README.md).

```sh
curl -fsS http://127.0.0.1:8001/health/ready
```

The API starts directly with `uvicorn api.main:app --reload`, using read-only
source mounts for `api/` and `market/`. Python edits reload without rebuilding;
dependency changes need a rebuild, and `.env` changes need container recreation.
Open `http://127.0.0.1:8001/docs` and enter the key through **Authorize**. Never
put it in URLs, shell arguments or a public frontend bundle. Swagger/OpenAPI
remain accessible locally; versioned routes and metrics enforce authentication.

To run the API on the host instead of in Compose, keep the database running and
stop the Compose API first to avoid competing workers and port bindings:

```sh
docker compose stop api
poetry run uvicorn api.main:app --reload --reload-dir api --reload-dir market --host 127.0.0.1 --port 8001
```

`api/main.py` assembles the app, `api/settings.py` holds API-wide configuration
including the shared database fields, and `api/routes/v1/router.py` assembles
feature routers. Each feature has a `route.py`; public contracts live in
`api/schemas/`, and middleware lives under `api/routes/`. `api/models/` is reserved
for persistence models without replacing the existing native SQL repositories.

FastAPI's lifecycle currently owns the backtesting queue worker and optional ingestion task
in the same backend process, pending the separately authorized future node split.
The API imports `.env` through `env_file`, but
Compose clears `DATABASE_URL` and forces the local `DB_HOST`/`DB_PORT` so a hosted
URL cannot redirect research. Both database pools use SQLAlchemy 2.1.8 with
`pool_pre_ping=True`; psycopg retains native SQL, transactions, and COPY support.
Database state and backtest artifacts use separate named volumes.
Compose forces ingestion, automatic backfill and streaming off during the
provider hold. Enabling provider access requires separate authorization. Follow
the research guide to run a synthetic batch or freeze existing local bars.
[Offline research backend](docs/research-backend.md).

Leave the local stack running after smoke checks. Pause it with
`docker compose stop`; named volumes are retained. Use `docker compose up --build`
after dependency or image changes. Existing database volumes are not adopted, reset or
deleted. The local stack does not import hosted data.
[PostgreSQL Docker image](https://github.com/docker-library/docs/tree/master/postgres).

## Watchlists and automatic history

The database stores the active watchlist and its revision. While the worker runs,
it checks for changes every 30 seconds and automatically backfills the new watchlist
before restarting subscriptions. Existing completed symbol ranges are reused, so
adding a symbol does not download every unchanged symbol again.

```sh
poetry run bagel-market-data watchlist
poetry run bagel-market-data watchlist --symbols SPY,QQQ,IWM,VXUS,GLD
```

A `.env` watchlist edit is detected by the native worker too. For Compose/GCP,
recreate the container with the updated environment, or use the database-backed CLI.
An unchanged startup configuration never overwrites a watchlist saved through the
CLI. Order-only changes do not trigger a new load. With no running worker, changes
remain pending until the worker starts. `BAGEL_AUTO_BACKFILL=false` disables automatic
history loading when intentionally running a bounded validation.

A database trigger also normalizes direct Supabase Studio/SQL edits and marks a
changed watchlist pending automatically; callers do not need to increment its
revision themselves. The trigger queues work; the Python worker performs the download.

## Dates, resume, and corrections

```sh
poetry run python tools/backfill.py --start 2021-10-24 --end 2026-10-24 --dry-run
poetry run python tools/reconcile.py --start 2026-10-20 --end 2026-10-22 --dry-run
poetry run bagel-market-data check-config --online
```

Date-only boundaries mean NY midnight; timestamps require an offset. All intervals
are **start-inclusive, end-exclusive**, converted to UTC. The adapter translates
Alpaca's inclusive `end`. Default history ends at the latest completed minute
older than the configured historical-access delay (15 minutes by default).
The start defaults to five years before that end. Explicit larger ranges are
rejected unless `BAGEL_MAX_HISTORY_YEARS` is deliberately increased. This cap does
not expire or delete stored data.

Backfills paginate every symbol, checkpoint UTC-day chunks, and resume by skipping
committed ranges. A failed chunk leaves pending work; its bars and completion
checkpoint commit together. `reconcile` deliberately re-queries completed ranges.
The worker refreshes the calendar each minute during collection and checks the market
clock during regular hours for unexpected closures. Closed periods back off from
30 seconds to five minutes, waking before the next extended-session opening.
The worker performs incremental repair every five minutes and a seven-day
correction sweep every six hours, including when the live session is closed.

The same source/feed/instrument/interval/currency/adjustment identifies a series.
The database keeps one latest bar per series/timestamp. Updated stream bars
outrank initial bars; historically eligible REST snapshots outrank both. Within
an origin class, a later observation wins; REST uses request-start time so an
older slow request cannot overwrite a newer snapshot. Historical and live feeds
never silently switch. Corporate-action adjustments are intentionally separate
from the raw series.

These are **latest revised bars**, not a point-in-time revision archive. Future
backtesting work must account for that distinction. Provider symbol mapping uses
the current lookup date consistently across a historical load. Internal
instrument IDs survive observed renames and distinguish symbol reuse. Additional
vendors can explicitly map their identifiers to existing canonical instrument IDs;
ticker text alone never merges securities.

## Database operations

Native monthly partitions work on PostgreSQL 17 without TimescaleDB. A restricted
hosted writer can invoke a bounded partition-creation function but cannot issue
arbitrary DDL. The intentionally privileged local superuser bypasses RLS; do not
treat the local connection as a hosted permission check. All application tables,
bar partitions and supporting functions live in `public`; RLS and grants remain in DDL.
no frontend Data API access is granted. A dedicated connection holds the stream
lock, and losing it stops the worker. Persisted REST coverage repairs buffered
data lost in a forced termination or extended outage.

Add schema changes to the next sequential `database/ddl/NNNN.name.sql` file.
The first four bootstrap files preserve `main`'s original SQL; the fifth retains
the research tables from `dev`. File `0006` consolidates those historical schemas
into `public`, preserving data and renaming the two run tables to `ingestion_runs`
and `research_runs`. See the [existing local database upgrade](database/README.md#subsequent-changes).
Do not rewrite existing DDL or replay bootstrap
files against an existing database. The custom migration helper has been removed.

Hosted migrations and releases remain on hold. The existing Supabase GitHub
integration expects the old timestamped migration path; renaming the repository
directory does not reconfigure it. Resolve future production migration delivery
separately before merging a release. The deployed worker retains its restricted
`bagel_gcp_worker` login and existing Secret Manager credentials and schedule.
See [hosted migration history](docs/supabase-cd.md).

The Supabase Free project has a 500 MB database allowance. Automatic loads and the
`backfill` command start at five years and retry **3 → 2 → 1 years** when needed.
`BAGEL_DATABASE_BUDGET_MB=450` leaves 50 decimal MB for recovery and small live batches
before the provider's read-only quota is reached. Size is measured between bounded
day chunks, including indexes and other tables. No paid upgrade is performed.

A capacity retry truncates Bagel bar partitions, clears their coverage/repair
checkpoints, and reloads the shorter window. It preserves instruments, source/series
metadata, schema, watchlists, and audit runs. The private reset function verifies
the budget and watchlist revision, and refuses to truncate if another source/feed
has stored bars. If one year cannot fit, loading stops with a failed status instead
of repeatedly destroying data. Network/authentication errors never trigger a reset.

Backfills keep at most four day ranges in flight (`BAGEL_BACKFILL_WORKERS`) and
use client-side COPY into a private temporary staging table for larger batches.
All in-flight transactions finish before a capacity reset. Incremental repair
remains ordered so its durable cursor cannot jump over a failed range.

The accepted window is persisted and resumed after interruption. A live worker also
rechecks capacity and enters this recovery path as data grows. A stale writer cannot
repopulate data older than the retained history floor during a reset.

For a pilot database configured in `DATABASE_URL`, run
`poetry run python tools/estimate_storage.py`. It measures stored rows plus partition
and index sizes, then calculates a conservative watchlist capacity bound. A useful
pilot should contain enough bars to amortize empty index/page overhead. WAL,
backups, metadata, and future sources need additional headroom.

## Health and verification

The local backend exposes `/health/live`, `/health/ready`, and `/metrics` on
localhost port 8001. Readiness includes its background workers and reports
ingestion as disabled during offline research. The existing standalone cloud
collector retains its probes on port 9090. A closed market is healthy without a stream. Logs are JSON and omit
credentials, response bodies, and raw database exceptions. Metrics include stream
connectivity, received/written bars, write failures, buffering, ingestion lag,
repair backlog, pending ranges, and the last successful reconciliation.

```sh
poetry run ruff check .
poetry run ruff format --check .
poetry run pyright
```

CI runs these static checks, validates Compose and Terraform configuration, builds
both targets from `api/dockerfile`, checks OpenAPI generation, and runs the
collector's backfill dry-run. The `collector` target preserves the existing hosted
entry point and port-9090 probes; the `api` target starts Uvicorn. CI does not start a database or run a
maintained test suite. Temporary local smoke verification may use synthetic data;
do not call trading/order APIs or write to hosted Supabase for verification.

See [project documentation](docs/README.md), [GCP operations](docs/operations.md),
and [market backend notes](market/README.md) for the deployed setup and extension points.
The [platform plan](docs/platform-plan.md) describes future whole-market collection,
Redis buffering, a separate database writer and a combined strategy/risk/execution
node. Those services are not part of the API restructuring work.

## API references

- [Alpaca historical bars](https://docs.alpaca.markets/us/reference/stockbars)
- [Alpaca stock streaming and updated bars](https://docs.alpaca.markets/us/docs/real-time-stock-pricing-data)
- [Alpaca WebSocket authentication and limits](https://docs.alpaca.markets/us/docs/streaming-market-data)
- [Alpaca Python request models](https://alpaca.markets/sdks/python/api_reference/data/stock/requests.html)
- [Supabase PostgreSQL connections](https://supabase.com/docs/guides/database/connecting-to-postgres)
- [Supabase native partitioning on PostgreSQL 17](https://supabase.com/docs/guides/database/migrating-to-pg-partman)
