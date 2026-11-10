# Database and completed history

The hosted Supabase project is `iitmbhpmpngtjrqhhown` (AWS us-east-1), PostgreSQL 17.
Hosted schema history is unchanged. Local development now uses standard
PostgreSQL 17 and sequential files in `database/ddl/`:

| Canonical migration | Purpose |
|---|---|
| `0001.market_data_foundation.sql` | Instruments, mappings, sources, series, partitioned bars, runs/ranges/cursors, restricted role, maintenance function |
| `0002.watchlist_capacity_backfills.sql` | Watchlist revisions, capacity resets, bounded recovery and history floor |
| `0003.watchlist_change_trigger.sql` | Normalize direct watchlist edits and queue historical reloads |
| `0004.github_cd_smoke_test.sql` | Preserve the existing owner-only deployment marker |
| `0005.research_backend.sql` | Offline datasets, durable runs/attempts, and batch membership |
| `0006.consolidate_public_schema.sql` | Move application objects into `public` and remove emptied domain schemas |

The first four files preserve SQL read from `main`; only filenames changed.
The fifth retains the research schema already on `dev`. PostgreSQL runs this DDL
in filename order through its native Docker entrypoint on an empty data volume.
New files are not automatically applied to populated volumes. The old custom
migration helper is removed; no local Supabase migration ledger is required.
[DDL provenance and initialization lifecycle](../database/README.md).

The hosted Supabase migration ledger and private legacy checksum audit are left
untouched. Production migrations remain on hold. The existing GitHub integration
expects the former timestamped path, not this new bootstrap directory; resolve
future release delivery separately. Never replay these files against hosted data.
[Hosted history and release hold](supabase-cd.md).

All local application tables and functions now live in **`public`**. The two run
tables are `ingestion_runs` and `research_runs`; all other table names are retained.
Monthly partitions and the legacy `migrations` audit table move too. Earlier DDL
remains unchanged; migration `0006` preserves data and dependencies, updates
function bodies and removes only emptied application schemas. Hosted objects are
unchanged until a separately authorized release. Local SQL inspection:

```sql
select table_schema, table_name from information_schema.tables
where table_schema = 'public'
order by 1, 2;
select * from public.watchlists;
select pg_database_size(current_database());
```

Monthly range partitions use `(series_id, timestamp)` identity with typed decimal
prices, nonnegative volume/counts, OHLC checks, and time-range indexes. Native
partitioning avoids a TimescaleDB dependency. Historical partitions are created
before writes; a bounded private maintenance function creates future partitions.
Restricted hosted writers cannot issue arbitrary DDL. RLS/grants protect application tables. Partition
children have no direct client policies; access goes through the parent table.

The initial five-year IEX download completed without a capacity reset:

| Symbol | Unique minute bars |
|---|---:|
| SPY | 494,493 |
| QQQ | 487,975 |
| IWM | 463,994 |
| VXUS | 173,487 |
| **Total** | **1,619,949** |

The observed history runs from October 25, 2021 through October 23, 2026. The
initial database footprint was approximately 312 MB including indexes. Fifteen
sampled bars matched fresh Alpaca responses exactly. Checkpoint/audit row sums can
exceed unique bars after overlapping resumed chunks; use the bars table for counts.
Live repairs can subsequently increase these counts and storage.

The Free allowance is 500 MB. Bagel checks a 450 decimal MB budget, preserving
headroom. If an automatic load or accumulated live data reaches the budget, it
tries history windows 5 → 3 → 2 → 1 years. This user-authorized capacity recovery
truncates only Bagel bars and coverage/repair checkpoints, preserves schema,
instruments, sources, series, watchlist and audit history, then reloads the shorter
window. It refuses to reset if another provider/feed has bars, and stops if one
year cannot fit. Network or authentication errors do not trigger truncation.
The ordinary five-year initial range limit is not a rolling retention policy.

Change local symbols with the CLI or update `public.watchlists.symbols` through SQL.
The database trigger normalizes and increments the revision; a running worker
polls every 30 seconds, backfills pending additions, and updates subscriptions.
Order-only edits do not cause a reload. A stopped worker handles pending changes
on its next start. Existing completed symbol ranges are reused.

CI does not start a database or run database integration tests. Temporary database
verification must use isolated local resources and synthetic data, never hosted
database URLs. Compose credentials are runtime-injected secrets, including local
ones. The explicitly authorized local superuser bypasses RLS; validating that
connection does not establish the safety of a restricted hosted login.
