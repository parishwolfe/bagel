# PostgreSQL DDL

Local development uses standard PostgreSQL 17, not the Supabase platform. The
hosted database remains Supabase PostgreSQL. From the repository root, start
PostgreSQL and the single Bagel API with `docker compose up`.

All current application tables and functions live in `public`, including monthly
bar partitions and the legacy `migrations` audit table. The two formerly named
`runs` tables are `public.ingestion_runs` and `public.research_runs`. Domain
separation remains in Python modules, not separate database schemas.

## Initialization

Compose mounts `ddl/` read-only at `/docker-entrypoint-initdb.d/`. PostgreSQL's
official entrypoint applies the SQL in filename order **only when its data
directory is empty**. No separate migration service, Supabase CLI, or application
startup DDL is involved. PostgreSQL reads its initial database, superuser and
password from `DB_DATABASE`, `DB_USER` and `DB_PASSWORD` through Compose; tracked
files contain no credential values or fallback passwords.

The local superuser connection is intentional. The SQL still retains its
backend roles, grants, RLS and bounded maintenance functions for compatibility
with restricted hosted logins. The API has no authentication, is published only
on loopback, and refuses remote research database hosts. Do not deploy this local
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
Supabase infrastructure initialization SQL is not needed. The former Python
migration helper's native ledger and login-provisioning code are not needed for
fresh Docker initialization and have been removed.

## Subsequent changes

Add the next sequential `NNNN.name.sql` file; do not rewrite existing files.
Restarting a populated PostgreSQL volume **does not apply new files**. Existing
databases need an explicitly authorized, reviewed forward migration, or an
explicitly approved fresh disposable volume. Never delete data to make startup
pass, and never replay bootstrap SQL against the hosted database.

For an existing local database still on files `0001`–`0005`, stop the API before
applying `0006` **once** with the database owner's connection. The migration is
transactional and has a bounded lock wait. Do not replay the first five files.

```sh
docker compose stop api
docker compose exec -T db psql -X --set=ON_ERROR_STOP=1 < database/ddl/0006.consolidate_public_schema.sql
docker compose up --build -d
```

The database container supplies its configured user/database through runtime
environment variables, so no credentials appear in these command arguments.

Renaming the directory does not reconfigure the hosted Supabase GitHub
integration or rewrite its timestamped migration history. That integration used
`supabase/migrations/`, not `database/ddl/`. Production migration delivery must be
resolved separately before a future release; it is on hold. Hosted settings,
credentials, existing data and the collector's automation schedule are unchanged.

See the root [startup instructions](../README.md#local-postgresql-stack) and the
[official PostgreSQL image initialization documentation](https://github.com/docker-library/docs/tree/master/postgres#initialization-scripts).
