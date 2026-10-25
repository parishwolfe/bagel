# Bagel documentation

Bagel currently ingests market data only. It does not create orders, run strategies,
or manage portfolios. Alpaca credentials belong to a paper account.

- [Architecture and data contracts](architecture.md)
- [Database, migrations, and completed history](database.md)
- [GCP deployment and operations](operations.md)
- [Credentials and access](security.md)
- [Session record and verification](session-record.md)

The repository root README covers Python/Poetry, the `.venv`, Compose, commands,
configuration, and local development. `supabase/migrations/` is the canonical DDL.
