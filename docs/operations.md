# GCP deployment and operations

## Deployment inventory

Project: `bagel-512620` (project number `400945978394`), region `us-east4`.
The signed-in gcloud user deploys through local Application Default Credentials;
this deployment does not use HCP Terraform or a Terraform Cloud workspace.

| Resource | Name/location |
|---|---|
| Remote state | `gs://bagel-512620-tfstate/terraform/market-data/default.tfstate` |
| Build source | `gs://bagel-512620-build-source/source/` (seven-day expiry) |
| Image repository | `us-east4-docker.pkg.dev/bagel-512620/bagel/market-data` |
| Worker pool | `bagel-market-data`, one instance, 1 vCPU / 512 MiB |
| Jobs | `bagel-backfill`, `bagel-reconcile`, single task each |
| Runtime account | `bagel-market-data@bagel-512620.iam.gserviceaccount.com` |
| Build account | `bagel-build@bagel-512620.iam.gserviceaccount.com` |

Worker pools run continuous background processes without a public HTTP endpoint.
The instance remains allocated and billable while it sleeps outside market hours.
Builds, storage, registry, and network traffic can also incur GCP charges. Supabase
remains on Free; no subscription upgrade was made. The worker runs paper market-data
credentials and has no order-submission code.

## Authentication and Terraform

From the repository root, with gcloud and Terraform installed:

```sh
gcloud config set project bagel-512620
gcloud auth application-default set-quota-project bagel-512620
terraform -chdir=infra/gcp init -backend-config=backend.hcl
terraform -chdir=infra/gcp plan -var-file=production.tfvars.example -out=deploy.tfplan
terraform -chdir=infra/gcp apply deploy.tfplan
```

If ADC is absent, use `gcloud auth application-default login` interactively first.
Do not use access-token printing as a connection test. `terraform plan` exercises
ADC and backend access. The GCS backend provides state locking; object versioning
protects previous states. Do not apply this configuration with a different backend
or a local state. Do not run `apply` without the production variables: the default
`enable_runtime=false` is for initial provisioning only.

The initial state bucket was bootstrapped with gcloud (us-east4, uniform access,
public-access prevention, versioning) and imported into its own remote Terraform
state. Terraform prevents destroying it. There is no retention lock, which would
interfere with state-lock object deletion. APIs, registry, service accounts, IAM,
secret containers, jobs, worker pool, source bucket, and write-failure metric are
managed in that state. No database/password secret values enter Terraform.

`production.tfvars.example` contains the actual nonsecret project, image digest,
and secret version identifiers used for this deployment; copy/edit for later
releases. Local `*.tfvars`, state and plans are ignored. The provider is locked to
Google 7.22.0; Terraform 1.17.0 was used for this rollout.

## Build and release

Run the tests in the root README, review the diff, and audit source for credentials.
The upload list must contain no `.env`, credential JSON, state, plans, or caches:

```sh
gcloud meta list-files-for-upload
gcloud builds submit . --config=infra/gcp/cloudbuild.yaml \
  --gcs-source-staging-dir=gs://bagel-512620-build-source/source \
  --region=us-east4 --project=bagel-512620 --async
```

Cloud Build uses `bagel-build`, builds Linux containers, logs to Cloud Logging,
and pushes a build-ID tag. Read `results.images` using `gcloud builds describe`,
then pin the returned `@sha256:` digest in the production variables. Before replacing
the singleton worker, pause it with a reviewed Terraform plan using
`-var=worker_instances=0`, apply that plan, and verify its advisory lock has released.
Next apply the new image **while keeping `worker_instances=0`** and wait for that
revision to become Ready. Finally apply the normal production plan (one instance),
without changing its image. Do not combine the image change with scaling up: Cloud
Run can briefly restart the previous ready revision. A bounded standby loop also
keeps new-revision liveness probes healthy while waiting up to five minutes for an
old owner to drain; it never opens a second stream. REST repair covers the pause. Terraform
rejects mutable runtime tags. Apply a reviewed saved plan. CI runs tests/build
checks only; GitHub pushes do not deploy or access production credentials.

For initial provisioning, apply with `enable_runtime=false`, then run:

```sh
poetry run python -m tools.provision_gcp_secrets \
  --project bagel-512620 \
  --pooler-host aws-0-us-east-1.pooler.supabase.com \
  --project-ref iitmbhpmpngtjrqhhown
```

This uses the local owner connection, creates/verifies a restricted login, rotates
its password, verifies session-pooler TLS/access, and submits the three values via
stdin. It prints only version numbers. Follow the rotation procedure in
[security](security.md) before re-running against an existing worker.

## Observe operation

```sh
gcloud run worker-pools describe bagel-market-data --region=us-east4 --project=bagel-512620
gcloud logging read 'resource.type="cloud_run_worker_pool"' \
  --project=bagel-512620 --limit=30 --format=json
```

Private container probes use port 9090, `/health/live`; `/health/ready` includes
DB/stream state and is healthy without a stream when collection is paused.
`worker_heartbeat` logs every minute. `collection_paused` records a closure reason
and next connection time; `stream_connection` records authenticated subscriptions;
`reconciliation_complete` proves completed REST correction sweeps. Watch for
`batch_write_retry`, `stream_reconnect`, failed runs, and a growing repair cursor
lag. Never paste full configuration or secret-access output into logs or issues.

`/metrics` exposes connectivity, queue depth, writes/failures, ingestion lag,
repair backlog, pending ranges, and last reconciliation time. These Prometheus
metrics require a private scraper to become Monitoring time series. Cloud Run
platform metrics and the logs-based `bagel/write_failures` metric are available
without one. No notification channel or paging destination has been configured.

The worker rechecks holidays/closures and waits automatically, then subscribes
before 04:00 NY. An authenticated connection is not evidence that bars were
published; IEX can be quiet, especially during extended hours. REST repair uses
the same feed and never fabricates bars. See [architecture](architecture.md).

## Watchlists and jobs

Use the root CLI with an appropriate database connection, or edit the private
watchlist table in Supabase Studio. The hosted worker notices within 30 seconds,
resumes history from durable checkpoints, and restarts subscriptions. A watchlist
change may invoke the documented capacity reset sequence if the free budget is
exceeded; monitor storage before adding many instruments.

The backfill Job defaults to a dry-run. A real bounded execution can override its
arguments, for example:

```sh
gcloud run jobs execute bagel-backfill --region=us-east4 --project=bagel-512620 \
  --args=backfill,--start,2026-10-20,--end,2026-10-22 --wait
gcloud run jobs execute bagel-reconcile --region=us-east4 --project=bagel-512620 --wait
```

Do not run a competing stream owner. Avoid simultaneous maintenance loads and
worker watchlist changes; shared locks/checkpoints protect recovery but work can
be redundant. The full initial five-year load is already complete.

## Stop, recover, and roll back

For an immediate pause, scale the worker pool to zero with gcloud or the GCP
console. This intentionally causes Terraform drift; the next production apply
restores one instance. Wait for the old singleton lock to release. To roll back,
set the previous immutable image digest and compatible secret versions, review
and apply a saved plan. Keep the database and schema; the durable REST cursor
repairs the outage. Never delete state, truncate tables, or run `terraform destroy`
as a release rollback. Worker/job deletion protection is enabled.

Restore state versions only after checking no Terraform process holds a lock.
Do not force-unlock an active apply. Database capacity errors, provider errors,
and credential rotation need separate diagnosis; only measured capacity invokes
Bagel's automatic shorter-history reload.

References: [GCS backend](https://developer.hashicorp.com/terraform/language/backend/gcs),
[worker pools](https://docs.cloud.google.com/run/docs/deploy-worker-pools),
[worker probes](https://docs.cloud.google.com/run/docs/configuring/workerpools/healthchecks),
[build identities](https://docs.cloud.google.com/build/docs/securing-builds/configure-user-specified-service-accounts),
[Supabase connections](https://supabase.com/docs/guides/database/connecting-to-postgres).
