# Supabase GitHub continuous deployment

The Supabase dashboard integration is connected to `parishwolfe/bagel`, working
directory `.`, production branch `main`, with Deploy to production enabled.
Preview branching is disabled on the existing Free plan. Production SQL deployment
uses the Supabase integration; our GitHub Actions workflow validates code and
Terraform but does not deploy GCP or connect to the hosted database.

## Migration ownership

`supabase/migrations/<version>_<name>.sql` is the schema source of truth.
`supabase_migrations.schema_migrations` is the authoritative applied-version history.
The first three files were renamed to the versions assigned when their identical
SQL was originally applied by the Supabase migration API:

| Previously named | Hosted/native version now used |
|---|---|
| `20261025010646_market_data_foundation.sql` | `20261025014047_market_data_foundation.sql` |
| `20261025014051_watchlist_capacity_backfills.sql` | `20261025014917_watchlist_capacity_backfills.sql` |
| `20261025020433_watchlist_change_trigger.sql` | `20261025020628_watchlist_change_trigger.sql` |

The SQL was compared directly with the hosted history and was byte-identical.
No foundation SQL or existing market data needs to be reapplied. The existing
`bagel_meta.migrations` audit remains preserved. `tools/migrate.py` is now local-only:
it uses native history for completion, checks local cached checksums when present,
and adopts verified legacy entries transactionally without replaying their DDL.
It never applies hosted migrations.

`supabase/config.toml` explicitly sets PostgreSQL 17, enables migrations, and
turns off seeding. No `[remotes.main]` block is declared, so production Auth/API/
Storage settings stay managed in the dashboard rather than inheriting CLI defaults.
No access tokens, database passwords or application secrets are stored in this file.
No GitHub deployment credentials were added for this integration.

## Change workflow

1. Create a branch and run `npx --yes supabase@2.122.0 migration new <name>`.
2. Add forward-only SQL. Private application schemas and appropriate RLS/grants
   remain required; the integration runs with schema-management privileges.
3. Run the local PostgreSQL integration suite and static checks. Review the PR.
4. Merge into `main`. Supabase's GitHub integration applies pending SQL versions.
5. Wait for deployment, then query the native ledger and the changed schema.
   Do not apply the same SQL manually as a substitute for a failed CD deployment.
6. Diagnose integration logs/history mismatches if deployment fails; use a new
   corrective migration for schema changes. Never reset production to retry CD.

## End-to-end verification table

The `github_cd_smoke_test` migration creates `public.bagel_cd_smoke_test` with one
marker containing its migration version and a database deployment timestamp.
RLS is enabled, no client policies are granted, and default privileges are revoked
from PUBLIC and Supabase client roles. The table is visible to database owners in
Studio and exists only to verify deployment. It contains no market data or secrets.
It is retained for inspection; remove it with a future migration if no longer needed.

Before merge, the hosted table was confirmed absent. Supabase CLI's hosted
`db push --dry-run --skip-vault` is used only to inspect pending versions; the test
migration must be applied by the integration after merge, not by our tooling.

Verification SQL for the owner SQL Editor:

```sql
select version, name from supabase_migrations.schema_migrations order by version;
select id, marker, deployed_at from public.bagel_cd_smoke_test;
select relrowsecurity from pg_class
where oid = 'public.bagel_cd_smoke_test'::regclass;
```

References: [GitHub integration](https://supabase.com/docs/guides/deployment/branching/github-integration),
[CLI config](https://supabase.com/docs/guides/local-development/cli/config).
