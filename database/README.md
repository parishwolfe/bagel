# PostgreSQL DDL

Local development uses PostgreSQL 17.11 with TimescaleDB 2.32.0, not the Supabase
platform. The custom image retains the existing PostgreSQL/Debian base and
populated volume. Hosted PostgreSQL remains Supabase. Start the complete local
platform from the repository root with `docker compose up`.

All current application tables and functions live in `public`, including monthly
bar partitions and the legacy `migrations` audit table. The two formerly named
`runs` tables are `public.ingestion_runs` and `public.research_runs`. Domain
separation remains in Python modules, not separate database schemas.

## Initialization

The `migrate` service is the sole DDL runner. It mounts `ddl/` read-only, holds an
advisory lock, and transactionally applies sequential files using SHA-256 checksums
and `public.bagel_schema_migrations`. Both fresh and populated local volumes are
supported. DDL is **not** also mounted into `initdb`.
PostgreSQL reads its initial database, superuser and
password from `DB_DATABASE`, `DB_USER` and `DB_PASSWORD` through Compose; tracked
files contain no credential values or fallback passwords.

The local superuser connection is intentional. The SQL still retains its
backend roles, grants, RLS and bounded maintenance functions for compatibility
with restricted hosted logins. The API requires a temporary shared API key, is
published only on loopback, and refuses remote research database hosts. Do not deploy this local
configuration as a production access-control model.

## Provenance

The first four files preserve the SQL from local `main` at commit
`d7e9b231fae9e969b1e9c99c922a7b761416172a`, inspected with read-only Git operations.
Only their filenames changed:

| Bootstrap file | Original `supabase/migrations/` file |
|---|---|
| `0001.market_data_foundation.sql` | `20261025014047_market_data_foundation.sql` |
| `0002.watchlist_capacity_backfills.sql` | `20261025014917_watchlist_capacity_backfills.sql` |
| `0003.watchlist_change_trigger.sql` | `20261025020628_watchlist_change_trigger.sql` |
| `0004.github_cd_smoke_test.sql` | `20261025031941_github_cd_smoke_test.sql` |
| `0005.research_backend.sql` | `20261109160028_research_backend.sql` (existing `dev` work) |

`0006.consolidate_public_schema.sql` is a forward migration: it moves application
tables, partitions, owned sequences/indexes and functions into `public`, resolves
the duplicate `runs` table/index names, and rewrites schema-qualified function
bodies. Table data, dependencies, grants, RLS and trigger bindings are preserved.
The four emptied application schemas are dropped with `RESTRICT`, not `CASCADE`;
unexpected remaining objects cause the transaction to fail rather than be deleted.
System schemas are not changed.

The fourth file preserves the owner-only historical deployment marker; it does
not enable continuous deployment. The fifth adds the datasets, durable job
queue, leases, batch membership, grants and RLS required by the current backend.
Supabase platform initialization is not needed. The current local-only
`market/migrations.py` runner does not provision credential-bearing logins. It
adopts only a recognized baseline after checking its actual objects; an unknown
partial initialization fails instead of guessing or replaying historical SQL.

## Subsequent changes

Add the next sequential `NNNN.name.sql` file; never rewrite an applied file.
`docker compose up` runs migrations before dependent services start. Repeated
runs verify checksums without reapplying completed files. Stop affected workers
for incompatible changes: automatic migration does not make arbitrary future
DDL online-safe. Never reset/delete a volume or replay bootstrap SQL to migrate.

Files `0007` onward add the revision hypertable, retained event journal, provider
jobs, experiment queue, account ledger/audit, and leased strategy actors. Existing
native monthly latest-bar partitions remain alongside the revision hypertable.
There is no automatic archive/journal pruning: pinned datasets and recovery must
be accounted for before a future retention change.

The database container supplies its configured user/database through runtime
environment variables, so no credentials appear in these command arguments.

Renaming the directory does not reconfigure the hosted Supabase GitHub
integration or rewrite its timestamped migration history. That integration used
`supabase/migrations/`, not `database/ddl/`. Production migration delivery must be
resolved separately before a future release; it is on hold. Hosted settings,
credentials, existing data and the collector's automation schedule are unchanged.

See the root [startup instructions](../README.md#start-the-local-platform) and the
[official PostgreSQL image initialization documentation](https://github.com/docker-library/docs/tree/master/postgres#initialization-scripts).
