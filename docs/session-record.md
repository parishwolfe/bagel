# Implementation and deployment record

This records the foundation work and subsequent deployment authorized in this
conversation. Work began October 24, 2026 (America/New_York); migration/build and
GCP timestamps are October 25 in UTC. No secret values are recorded here.

## Foundation delivered

- Created the Python 3.12 monorepo component with domain models, provider ports,
  Alpaca adapter, historical workflows, repository, configuration, runtime and CLI.
- Added Poetry configuration/lock, Python-created `.venv`, nonroot multi-stage
  Dockerfile, Compose PostgreSQL 17, migration service and tools profile.
- Added `run`, `backfill`, `reconcile`, `check-config`, and watchlist commands,
  plus migration/storage-estimate tools and a credential-free `.env.example`.
- Implemented decimal normalization, complete multi-symbol pagination, bounded
  retries/rate limiting, UTC timestamps, NY session classification, transactional
  writes/checkpoints, interrupted-load resume, corrections and duplicate handling.
- Added native monthly partitions, provider/feed-isolated series, instrument
  mappings, private ingestion state, checksummed migrations, RLS/grants,
  singleton locking, future partition maintenance and structured observability.
- Persisted all three DDL migrations in hosted Supabase and in the repository.
  Tables are in `market_data` and `ingestion`; an empty `public` schema is expected.
- Added persistent watchlist revisions and a SQL trigger so Studio/CLI symbol
  changes queue automatic resumable history loads. Existing ranges are reused.
- Added measured 450 MB capacity protection and the authorized 5 → 3 → 2 → 1-year
  fallback. Resets are guarded, preserve schema/metadata/audit history, reject
  mixed-source destruction, and stop if the one-year window cannot fit.
- Completed the entire initial five-year IEX load: **1,619,949 unique bars**,
  approximately **312 MB** total database size. No fallback/reset was needed.
  All four original symbols are ready at watchlist revision 1. Sample verification
  matched fifteen bars exactly against Alpaca. See [database details](database.md).

## GCP rollout

The user then authorized hosted deployment, secret provisioning and a GitHub push.
The initially selected gcloud project was unrelated; gcloud and ADC quota settings
were aligned with `bagel-512620`. Billing is enabled. Relevant GCP APIs were enabled.

A GCS bucket was bootstrapped with uniform access, enforced public-access prevention
and object versioning, then imported into Terraform's own remote state. Backend
initialization and subsequent plans/applies exercised ADC successfully. The state
object is `gs://bagel-512620-tfstate/terraform/market-data/default.tfstate`.
There is no HCP Terraform workspace; this is HashiCorp Terraform CLI with GCS locking.

Terraform provisioned Artifact Registry, a private expiring build-source bucket,
separate build/runtime accounts and scoped IAM, three Secret Manager containers,
a write-failure logging metric, one Cloud Run worker pool and two Cloud Run Jobs.
The worker is configured for 1 vCPU / 512 MiB in us-east4. Jobs reuse its image and
identity. The historical Job defaults to dry-run; no new full download was started.

A dedicated `bagel_gcp_worker` PostgreSQL login was created with an in-memory random
password and `bagel_ingester` membership. Provisioning was adapted to the hosted
administrator's restricted role-management permissions; elevated flags are checked
rather than altered through superuser-only operations. Session-pooler IPv4/TLS and
restricted access were verified. Only that runtime DSN and the two Alpaca credentials
were uploaded, through stdin, as secret version 1. The administrator password and
Supabase publishable key remain local. No service-account key was created.

Alpaca's official documentation distinguishes free live IEX from delayed/restricted
SIP history. The deployed collector deliberately keeps IEX to match existing data,
uses the paper Trading API for calendar/clock, and retains a 15-minute REST cutoff.
Native WebSocket authentication and subscriptions to both bars/updatedBars succeeded.
No order endpoint is implemented or called.

Calendar/clock gating was improved for unattended operation: the calendar refreshes
independently of arriving bars, regular-session unexpected closures pause the
stream, extended hours tolerate a false regular-market clock, and holidays/closed
periods back off to five minutes while waking before the next extended opening.
A deployed log confirmed a non-trading day and a next connection time of
**2026-10-26 07:59 UTC / 03:59 EDT**, for the 04:00 extended-session opening.
Actual availability still depends on the selected exchange/feed publishing data.

The first cloud build omitted `poetry.toml` from the restrictive upload allowlist;
this was fixed before deployment. Build `7194d2e7-1d97-484c-a2ce-43104f3f1c9d`
produced the first successful image, and revision `bagel-market-data-00001-b4b`
passed Cloud Run startup/liveness probes. A later verification found that a newly
booted host's monotonic clock could postpone its first full correction sweep;
that was fixed with a regression test and rebuilt for the final release, Cloud
Build `8b2d8ac0-fb17-4fa0-988b-9cdcf34dd1e5` at digest
`sha256:ebdf1fa752e23a62855f0806f34f83c1289fe9709bf40b2fcaa5741bbfd2e48d`.
Verification also exposed Cloud Run restarting the previous ready revision when an
image change and scale-up happened together. The pool was paused through the
documented REST API because the local worker-update CLI lacked grpc. The pending
Terraform client was stopped gracefully; remote state retained all resources.
A bounded liveness-preserving standby loop and ownership-transfer regression test
were added. The final build is `f98e2153-2987-4af6-96b7-d9b738c4c00e`, digest
`sha256:eff35003a76cc0b790c39a594ebc3136b4b03919862cc79fb9195a6a741d8e95`.
Releases now pause, change the image while still paused, wait for revision readiness,
then scale up separately, preventing competing stream-lock owners. See the pinned production configuration for the
final image digest and [operations](operations.md) for release commands.

## Verification

- **59 tests passed** with disposable local PostgreSQL integration enabled.
  Coverage includes pagination/empty results/429s/auth failures, stream updates and
  disconnects, dates/DST/holidays/early closes, emergency gates, quiet-stream shutdown,
  startup reconciliation, source isolation, partition rollover, correction order,
  transactional resume, watchlist triggers, capacity retries and safe failure.
- Ruff lint/format and Pyright passed. Terraform provider schema validation passed.
  Poetry lock checks and Compose/image smoke validation were performed during the
  foundation work. Integration tests refuse remote database hosts.
- A GCP execution of `check-config --online` succeeded:
  `bagel-backfill-8fx4q`. It verified Alpaca and the hosted schema from the deployed
  image/identity and wrote no market data.
- Cloud Run reported Ready, successful startup/liveness probes, and a subsequent
  heartbeat with `database_ok=true`, `session_active=false` during the weekend.
  The durable repair cursor advanced; no pending ranges remained at inspection.
- A post-deployment Terraform plan reported no infrastructure changes.
- An in-memory audit compared repository files and pulled remote state against
  actual local/GCP secret values (including the generated database password), with
  zero matches. The Cloud Build upload list had zero forbidden files. Secret values
  were not printed or written into source, plans, Docker layers or documentation.

The local PostgreSQL volume is retained. Future frontend, strategy, backtesting,
portfolio risk and order execution components have not been implemented. No
Supabase plan upgrade, paid market-data subscription or live-money trading was
performed. No actual Monday trading-session arrival can be observed during this
weekend deployment; the worker remains running and scheduled to collect available
IEX data automatically. Notification/paging channels and a private Prometheus
scraper remain future operational additions.

## Final deployed state

Revision **`bagel-market-data-00003-vbb`** is Ready on the final image, with one
instance restored after the separate paused-image update. Startup and liveness
probes passed. Its initial seven-day correction sweep completed at
**2026-10-25 02:52:37 UTC**, and the database had zero pending ranges. The measured
footprint after correction updates was **313,439,923 bytes** (approximately 313 MB),
below the 450 MB recovery budget. The weekend closure log again scheduled
connection for Monday October 26 at 03:59 EDT.

The final-image online diagnostic also succeeded as execution
`bagel-backfill-5k4zf`; it performed API/schema checks without market-data writes.

The first GitHub Actions run passed Terraform validation but found a service-container
health-command quoting issue before Python tests started. The runner's argument
parser requires double quotes around the PostgreSQL health command; the workflow
was corrected and pushed separately. Local PostgreSQL tests had already passed.

The corrected GitHub run `40156223710` passed both Python/container checks and
Terraform validation. GitHub then reported development-only pytest advisory
[CVE-2025-71176](https://github.com/advisories/GHSA-6w46-j5rx-g56g). Poetry updated
pytest from 8.4.2 to 9.1.1 and raised its minimum to 9.0.3. No runtime dependency
versions changed; the production image installs only the main dependency group.
All 59 local PostgreSQL tests passed again with the patched test runner.
