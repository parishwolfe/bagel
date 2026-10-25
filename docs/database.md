# Database and completed history

The hosted Supabase project is `iitmbhpmpngtjrqhhown` (AWS us-east-1), PostgreSQL 17.
DDL is persisted in PostgreSQL and versioned in `supabase/migrations/`:

| Canonical migration | Purpose |
|---|---|
| `20261025010646_market_data_foundation.sql` | Instruments, mappings, sources, series, partitioned bars, runs/ranges/cursors, restricted role, maintenance function |
| `20261025014051_watchlist_capacity_backfills.sql` | Watchlist revisions, capacity resets, bounded recovery and history floor |
| `20261025020433_watchlist_change_trigger.sql` | Normalize direct watchlist edits and queue historical reloads |

The initial application used the Supabase migration API, whose recorded timestamps
are `20261025014047`, `20261025014917`, and `20261025020628`, with the same migration
names. `bagel_meta.migrations` additionally records the canonical filenames and
SHA256 checksums. Use `tools/migrate.py` for subsequent migrations; do not mix its
history with `supabase db push`. Never edit a migration that has already run.

Tables intentionally live in **`market_data`** and **`ingestion`**, not `public`.
In Supabase Studio's Table Editor, change the schema selector to see them. The
`bagel_meta` schema contains the private migration ledger. SQL Editor inspection:

```sql
select table_schema, table_name from information_schema.tables
where table_schema in ('market_data', 'ingestion', 'bagel_meta')
order by 1, 2;
select * from ingestion.watchlists;
select pg_database_size(current_database());
```

Monthly range partitions use `(series_id, timestamp)` identity with typed decimal
prices, nonnegative volume/counts, OHLC checks, and time-range indexes. Native
partitioning avoids a TimescaleDB dependency. Historical partitions are created
before writes; a bounded private maintenance function creates future partitions.
The worker cannot issue arbitrary DDL. RLS/grants protect backend schemas. Partition
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

Change symbols with the CLI or update `ingestion.watchlists.symbols` in Studio.
The database trigger normalizes and increments the revision; a running worker
polls every 30 seconds, backfills pending additions, and updates subscriptions.
Order-only edits do not cause a reload. A stopped worker handles pending changes
on its next start. Existing completed symbol ranges are reused.

Local integration tests always use temporary databases on loopback and refuse
hosted database URLs. Compose credentials are development fixtures only.
