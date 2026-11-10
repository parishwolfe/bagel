# Database development rules

Follow the repository-wide [development rules](../AGENTS.md) and
[database security guidance](../docs/security.md).

## Migrations and data safety

- Never spend cycles formatting SQL/configuration or running standalone formatting commands/checks unless explicitly requested. Follow the root formatting policy; report missing hook coverage rather than manually reformatting files. Never rewrite applied migrations for formatting.
- Keep canonical versioned DDL in `ddl/`, named sequentially `NNNN.name.sql`. The first four files preserve SQL from `main`; never edit an applied file. Add the next sequential file for a schema change. Docker runs these files in filename order only when initializing an empty database volume.
- The current application schema is `public`, including bar partitions and supporting functions. Do not add separate domain schemas. `0006` consolidates the historical schemas without changing earlier DDL; preserve the distinction between `ingestion_runs` and `research_runs`.
- Review migrations for locks, data loss, compatibility, and recovery implications. Ask before destructive schema changes or data operations; do not reset a database to make a migration pass.
- Use plain local PostgreSQL 17 for authorized development verification. No Supabase infrastructure or custom migration runner is needed. Never apply these bootstrap files to an existing hosted database.
- Hosted database changes are on hold. A merge to `main` can deploy migrations through the Supabase GitHub integration; do not push a production release path without explicit authorization.

## Access controls

- Use least-privilege schema, table, sequence, and function grants. Keep private backend data unavailable to anonymous/frontend roles; use RLS where applicable without treating it as a substitute for grants.
- Hosted runtime roles must not acquire owner, superuser, arbitrary DDL, or RLS-bypass privileges. The configured superuser is explicitly authorized for local development only; no role-provisioning password belongs in DDL.
- Prefer invoker-rights functions. Any required `SECURITY DEFINER` function must have an explicit safe search path, schema-qualified references, narrowly granted execution, and bounded validated inputs. Do not leave privileged functions executable by `PUBLIC`.
- Never embed credentials or secret values in migrations, seed files, snippets, or committed configuration. Preserve TLS requirements for remote connections.

## Verification

- Do not add or maintain unit tests or test infrastructure unless explicitly requested. Use temporary local verification where needed, with synthetic data and isolated disposable resources.
- Do not remove existing migrations, tests, or user data as cleanup. Report which local migration/access checks were actually performed.
