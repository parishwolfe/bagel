# Database development rules

Follow the repository-wide [development rules](../AGENTS.md) and
[database security guidance](../docs/security.md).

## Migrations and data safety

- Never spend cycles formatting SQL/configuration or running standalone formatting commands/checks unless explicitly requested. Follow the root formatting policy; report missing hook coverage rather than manually reformatting files. Never rewrite applied migrations for formatting.
- Keep canonical versioned DDL in `migrations/`. Add a new migration rather than rewriting an applied migration or modifying migration history/checksums.
- Review migrations for locks, data loss, compatibility, and recovery implications. Ask before destructive schema changes or data operations; do not reset a database to make a migration pass.
- Use the local Supabase stack for authorized development verification. The migration helper is local-only; never bypass its remote-host protections.
- Hosted database changes are on hold. A merge to `main` can deploy migrations through the Supabase GitHub integration; do not push a production release path without explicit authorization.

## Access controls

- Use least-privilege schema, table, sequence, and function grants. Keep private backend data unavailable to anonymous/frontend roles; use RLS where applicable without treating it as a substitute for grants.
- Runtime roles must not acquire owner, superuser, arbitrary DDL, or RLS-bypass privileges. Keep migration/provisioning credentials separate from application credentials.
- Prefer invoker-rights functions. Any required `SECURITY DEFINER` function must have an explicit safe search path, schema-qualified references, narrowly granted execution, and bounded validated inputs. Do not leave privileged functions executable by `PUBLIC`.
- Never embed credentials or secret values in migrations, seed files, snippets, or committed configuration. Preserve TLS requirements for remote connections.

## Verification

- Do not add or maintain unit tests or test infrastructure unless explicitly requested. Use temporary local verification where needed, with synthetic data and isolated disposable resources.
- Do not remove existing migrations, tests, or user data as cleanup. Report which local migration/access checks were actually performed.
