# Hosted Supabase migration history and release hold

The live database remains Supabase PostgreSQL. No hosted database, integration
setting, credential, collector deployment or automation schedule was changed by
the local PostgreSQL conversion. Production releases remain on hold.

## Historical deployment path

The recorded Supabase GitHub integration is connected to `parishwolfe/bagel`,
working directory `.`, production branch `main`, with Deploy to production
enabled. It expected timestamped SQL under `supabase/migrations/` and tracked
applied versions in `supabase_migrations.schema_migrations`. That hosted history
must not be renumbered, rewritten or replayed.

The first three foundation files previously adopted the versions assigned when
their identical SQL was originally applied through Supabase's migration API:

| Original identity | Hosted identity |
|---|---|
| `20261025010646_market_data_foundation.sql` | `20261025014047_market_data_foundation.sql` |
| `20261025014051_watchlist_capacity_backfills.sql` | `20261025014917_watchlist_capacity_backfills.sql` |
| `20261025020433_watchlist_change_trigger.sql` | `20261025020628_watchlist_change_trigger.sql` |

The hosted private `bagel_meta.migrations` audit is also preserved. The
owner-only `public.bagel_cd_smoke_test` marker records the historical integration
check; its DDL remains in the local baseline to preserve `main`'s SQL.

## Current local layout

`database/ddl/NNNN.name.sql` is now the local initialization source. The first
four files preserve SQL recovered from `main` using read-only Git operations;
the fifth retains existing `dev` research work. File `0006` moves local application
objects into `public` without altering those earlier SQL files or the hosted database. See
[provenance and initialization lifecycle](../database/README.md).

The Supabase CLI configuration, local platform services and custom migration
runner have been removed. The Docker entrypoint initializes only a fresh local
PostgreSQL volume; it neither connects to Supabase nor updates hosted ledgers.

**The existing hosted GitHub integration does not automatically discover this
new DDL layout.** Before any future release, explicitly approve and implement a
forward-only production migration delivery process that reconciles the existing
hosted versions. Do not merge a release, reconfigure the integration, run hosted
migration tools, or replay bootstrap SQL as part of local verification.

References: [Supabase GitHub integration](https://supabase.com/docs/guides/deployment/branching/github-integration),
[PostgreSQL initialization scripts](https://github.com/docker-library/docs/tree/master/postgres#initialization-scripts).
