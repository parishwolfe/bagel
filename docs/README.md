# Bagel documentation

Bagel currently supports market-data ingestion and offline strategy research
with simulated execution and portfolio accounting. Broker execution and the
operator UI are planned.

- [Architecture and data contracts](architecture.md)
- [Trading platform architecture and phased MVP plan](trading-platform-plan.md)
- [Run the offline research backend](research-backend.md)
- [Database, migrations, and completed history](database.md)
- [Supabase GitHub continuous deployment](supabase-cd.md)
- [GCP deployment and operations](operations.md)
- [Future cloud deployment and cost plan](cloud-deployment-plan.md)
- [Credentials and access](security.md)
- [Session record and verification](session-record.md)

The repository root README covers Python/Poetry, the `.venv`, Compose, commands,
configuration, and local development. `supabase/migrations/` is the canonical DDL.
