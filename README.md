# Bagel

Bagel is a monorepo for an algorithmic trading platform. The `market/` backend
contains US equity/ETF ingestion and an offline strategy research workflow.
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
| `market/` | One Python backend with internal modules for data, strategies, backtesting, risk, simulated execution, portfolio accounting, research, and the API |
| `ui/` | Reserved for the future Next.js application and its separate deployment |
| `tools/` | Backfill, reconciliation, migration, and storage-estimate entry points |
| `supabase/migrations/` | Canonical versioned PostgreSQL DDL |
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

Keep your existing `.env`. Use `.env.example` to add missing settings without
overwriting credentials. `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` are both required.
`ALPACA_ENDPOINT` is the Trading API endpoint used for assets and calendars;
market data always uses the documented data/stream endpoints. Paper credentials
can authenticate to those market-data endpoints; data entitlements still apply.

Supabase's publishable `SUPABASE_KEY` is **not** a PostgreSQL password. Ingestion
uses `DATABASE_URL`, or assembles a TLS connection from `SUPABASE_HOST`,
`SUPABASE_PORT`, `SUPABASE_DB`, `SUPABASE_USER`, and `SUPABASE_PASSWORD`.
Use a backend login that belongs to `bagel_ingester` for the deployed worker.
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
hooks. They use the settings in `pyproject.toml` to lint and automatically format
staged Python files on each commit. If formatting changes a file, review and stage
the changes, then retry the commit. Keep the hook revision aligned with Ruff in
`poetry.lock`. The all-files command lints and formats the whole repository on
demand.

## Local Supabase stack

Use Docker Desktop and the pinned Supabase CLI for the development stack. Run the
network creation once to give Supabase and the worker a shared bridge. On macOS,
Docker Desktop's network port binding preference controls whether the published
Supabase ports listen on localhost or all interfaces. Start Supabase from the
repository root so it applies the migrations in `supabase/migrations/`.

```sh
docker network create --opt com.docker.network.bridge.host_binding_ipv4=127.0.0.1 bagel-local-supabase
npx --yes supabase@2.122.0 start --network-id bagel-local-supabase
```

Supabase Studio is at `http://127.0.0.1:54323`, the API at `http://127.0.0.1:54321`,
and PostgreSQL at `127.0.0.1:54322`. With the Python environment set up, provision
the restricted research login. The migration helper recognizes the CLI's
applied migration ledger. The credentials below are local defaults.

```sh
MIGRATION_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:54322/postgres \
  .venv/bin/python -m tools.migrate --provision-research-role
docker compose up --build -d research-api research-worker
curl -fsS http://127.0.0.1:8001/health/ready
```

The default Compose services are the offline research API and worker. They
share the market image and a local artifact volume. The `ingest` profile is
reserved for separately authorized provider access. Follow the research guide
to run a synthetic batch or to freeze existing local bars after importing them.
[Offline research backend](docs/research-backend.md).

Leave the research stack running after development smoke checks. To pause it later,
run `docker compose stop research-api research-worker` and then `npx --yes supabase@2.122.0 stop
--network-id bagel-local-supabase` to retain the local Supabase data for restart.
[Supabase local development guide](https://supabase.com/docs/guides/local-development).

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

Native monthly partitions work on PostgreSQL 17 without TimescaleDB. The writer
can invoke a private, bounded partition-creation function but cannot issue
arbitrary DDL or delete data. RLS and explicit grants protect private schemas;
no frontend Data API access is granted. A dedicated connection holds the stream
lock, and losing it stops the worker. Persisted REST coverage repairs buffered
data lost in a forced termination or extended outage.

Create new migration files with `npx --yes supabase@2.122.0 migration new <name>`.
Hosted migrations deploy through the **Supabase GitHub integration** when a PR
merges into `main`. Use repository `parishwolfe/bagel`, working directory `.`, and
Deploy to production enabled for `main`. Supabase's native
`supabase_migrations.schema_migrations` is the authoritative applied-version ledger.
The initial migration filenames match the versions already applied by Supabase;
their SQL contents are unchanged. `supabase/config.toml` selects PostgreSQL 17,
enables migrations and disables seeds. Production Auth/API settings remain managed
in the dashboard because no production `[remotes]` configuration is declared.

`tools/migrate.py` is a **local-only migration and login provisioning helper** using that same native ledger.
It refuses remote database hosts. It safely adopts verified legacy local checksums
without reapplying the foundation DDL; `bagel_meta.migrations` remains an auxiliary
checksum audit, not an independent completion history. Keep database-owner
credentials out of application images and GitHub. The hosted worker uses the
dedicated `bagel_gcp_worker` login with membership in `bagel_ingester`; its
credentials are in Secret Manager. See [Supabase CD](docs/supabase-cd.md).

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

The worker exposes private `/health/live`, `/health/ready`, and `/metrics` routes
on port 9090. A closed market is healthy without a stream. Logs are JSON and omit
credentials, response bodies, and raw database exceptions. Metrics include stream
connectivity, received/written bars, write failures, buffering, ingestion lag,
repair backlog, pending ranges, and the last successful reconciliation.

```sh
poetry run ruff check .
poetry run ruff format --check .
poetry run pyright
```

CI runs these static checks, validates Compose and Terraform configuration, builds
the container, and runs a backfill dry-run. It does not start a database or run a
maintained test suite. Temporary local smoke verification may use synthetic data;
do not call trading/order APIs or write to hosted Supabase for verification.

See [project documentation](docs/README.md), [GCP operations](docs/operations.md),
and [market backend notes](market/README.md) for the deployed setup and extension points.

## API references

- [Alpaca historical bars](https://docs.alpaca.markets/us/reference/stockbars)
- [Alpaca stock streaming and updated bars](https://docs.alpaca.markets/us/docs/real-time-stock-pricing-data)
- [Alpaca WebSocket authentication and limits](https://docs.alpaca.markets/us/docs/streaming-market-data)
- [Alpaca Python request models](https://alpaca.markets/sdks/python/api_reference/data/stock/requests.html)
- [Supabase PostgreSQL connections](https://supabase.com/docs/guides/database/connecting-to-postgres)
- [Supabase native partitioning on PostgreSQL 17](https://supabase.com/docs/guides/database/migrating-to-pg-partman)
