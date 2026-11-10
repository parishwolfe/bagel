# Market backend

`market/` is one backend component. Its internal modules share types and ship
in one market image. `market/data/` owns ingestion. `market/strategies/`,
`market/backtesting/`, `market/risk/`, `market/execution/`, and `market/portfolio/`
implement the offline simulator. `market/research/` owns datasets and jobs,
and `market/api/` exposes research operations. Local Compose runs one `api`
container and one backend process. FastAPI's lifecycle starts the research worker
as bounded background work and optionally starts the ingestion task. Ingestion
is disabled by default during offline research. CPU-bound backtests run off the
API event loop. The existing standalone collector CLI remains available for the
currently deployed automation.

The current developer workflow uses synthetic data and plain local PostgreSQL 17.
`market/database.py` shares the five `DB_*` settings and SQLAlchemy engines with
`pool_pre_ping=True`. Existing psycopg SQL, transaction and COPY behavior is retained.
The local API intentionally has no authentication and uses the configured local
superuser; Compose publishes it only on loopback. Hosted PostgreSQL remains Supabase.
See the
[research runbook](../docs/research-backend.md) for commands and current limits.

`domain` owns validated immutable bars and UTC intervals. `ports` defines provider
capabilities. `providers.alpaca` uses alpaca-py request models and implements the
documented REST pagination and WebSocket protocol explicitly: this preserves
decimal JSON prices, makes authentication failures visible, and reports disconnects.

`historical` orchestrates bounded daily ranges. `storage` owns reference identity,
series isolation, atomic upserts/checkpoints, and session locks. `runtime` runs
collection, bounded buffering/batch writing, REST repair, and lock heartbeats as
separate tasks. `observability` contains health and operational metrics. `cli`
composes these pieces; tools never implement another ingestion path.

To add a provider, implement `HistoricalBars`, `InstrumentLookup`,
`TradingCalendar`, and `StreamingBars`; emit domain bars rather than SDK objects.
Register the source and map provider identifiers to canonical instruments using
`PostgresStore.resolve_series(..., canonical_instruments=...)`. Leave different
feeds in separate series. Add provider-contract fixtures before enabling it in
the composition layer. New event types such as quotes and trades should get
typed event tables rather than nullable columns in the bars table.

The first release intentionally has no event broker. PostgreSQL range checkpoints
plus provider replay are the recovery mechanism. When ingestion fans out to
multiple downstream consumers or tick volume grows, a durable broker can be
introduced between provider adapters and writers without changing the domain.
