# Future cloud deployment plan

Start with one operator, a low-cost Next.js UI, and a FastAPI service that
scales to zero. The dashboard should be usable during regular US equity market
hours. The initial plan allows a cold start on the first API call after idle.
Keep the market-data collector on its existing schedule so data continues to
arrive independently of dashboard visits.
[NYSE trading hours](https://www.nyse.com/trade/hours-calendars).

This plan is for a future authorized release. Work continues on `dev` using
Docker and local Supabase. UI scaffolding requires user authorization. Hosted
changes require local strategy tests, reproducible backtests, and explicit
release authorization. A merge to `main` can deploy hosted Supabase migrations.
[Project memory](../AGENTS.md) · [Trading platform plan](trading-platform-plan.md) ·
[Supabase deployment workflow](supabase-cd.md).

## First hosted slice

| Component | Initial cloud plan |
| --- | --- |
| Operator UI | Deploy `ui/` independently. Serve the Next.js dashboard shell as static assets where practical. Select the host after checking free-tier eligibility, TLS, authenticated routing, and usage limits. |
| Control API | Deploy the `market/` image as a Cloud Run service in `bagel-512620`, `us-east4`. Start with request-based billing, zero minimum instances, 1 vCPU, 512 MiB, and a low maximum instance count sized against the database connection budget. Bind the API process to Cloud Run's assigned `PORT`. |
| Market data | Continue the deployed `bagel-market-data` worker pool on its current schedule. Keep `bagel-backfill` and `bagel-reconcile` as separately invoked maintenance jobs. |
| Research | Keep cloud compute on demand. Enable hosted batch submission after a separately invoked worker can claim the durable queue, finish a bounded run, and recover an interrupted attempt. Keep metadata in Supabase and immutable datasets and large results in a private GCS artifact bucket when hosted research is authorized. |

The UI loads its shell, signs the operator in with Supabase Auth, and makes its
first `/v1` request when the dashboard needs live data. That request starts
FastAPI if Cloud Run has scaled to zero. Show a loading state during startup and
timestamps when presenting live data. Fetch on page open or manual refresh.
While live polling is enabled, bound its rate and pause it when the tab is
hidden. Cloud Run may retain an idle container briefly. With zero minimum
instances and request-based billing, compute charges cover active requests,
startup, and shutdown. [Cloud Run autoscaling](https://docs.cloud.google.com/run/docs/about-instance-autoscaling) ·
[Cloud Run billing settings](https://docs.cloud.google.com/run/docs/configuring/billing-settings) ·
[Next.js static exports](https://nextjs.org/docs/app/guides/static-exports).

Keep the UI and API under one public origin using an on-demand `/v1` proxy at
the host or in a thin Next.js handler. Restrict sign-in to the owner. The proxy
checks the operator's session, invokes the IAM-protected API using a short-lived
Google ID token in `X-Serverless-Authorization`, and forwards the operator's
Supabase access token in `Authorization`. FastAPI verifies the operator and
account permissions on every request. Use separate service identities and
scoped database access for the API and workers. Store runtime secrets in Secret
Manager and set `Cache-Control: private, no-store` on account-specific responses.
[Operator UI and control API](trading-platform-plan.md#13-operator-ui-and-control-api) ·
[Cloud Run service authentication](https://docs.cloud.google.com/run/docs/authenticating/service-to-service) ·
[Credentials and access](security.md).

## Cost targets and review

The user reported roughly **$3/month** for the existing Cloud Run workload on
November 9, 2026. Use it as a starting point and check the billing breakdown
before rollout. Target **$0 for UI hosting** within an eligible free tier and
**under $1/month in incremental API charges** for light single-operator use.
Cloud Run free-tier credits are shared across projects on the billing account.
[Cloud Run pricing](https://cloud.google.com/run/pricing).

| API configuration | Estimated Cloud Run cost before credits |
| --- | --- |
| Scale to zero | About **$0.55/month** for 100,000 non-overlapping requests at 200 ms each, plus startup and shutdown. |
| Warm during regular sessions | About **$1.93/month** at the idle rate for one minimum instance over 22 sessions of 6.5 hours, plus the difference between idle and active rates when serving requests. |
| CPU available throughout regular sessions | About **$9.78/month** for 143 hours of instance-based billing at one instance. This is the case to price if a process needs CPU between requests. |

These estimates use November 2026 list prices in `us-east4` for 1 vCPU and
512 MiB. Use scale-to-zero at launch. Review warm capacity only if measured
cold-start latency interferes with market-hour use. Budget separately for
builds, image and research artifact storage, logging, network egress, and any
hosting or domain charges. Check actual usage after the first week and first
billing cycle. Set a project budget alert at a threshold agreed before rollout
and review any sustained increase by service.
[Cloud Run pricing](https://cloud.google.com/run/pricing) ·
[Cloud Billing alerts](https://docs.cloud.google.com/billing/docs/how-to/budgets).

## Rollout sequence

1. **Validate locally.** Continue with synthetic fixtures until the approved
   after-hours data import. Run strategy tests and reproduce the baseline
   backtest from a frozen dataset. After UI authorization, verify sign-in,
   dashboard loading, permission checks, refresh, and sign-out in the local stack.
   [Local startup](../README.md#local-supabase-stack) ·
   [Research runbook](research-backend.md).
2. **Review the first cloud release.** Use separate Terraform deployments for
   frontend and backend. Review the image digest, runtime identities, routing,
   hosted Supabase permissions and migration impact, the estimated bill, and
   the rollback steps before requesting release approval. Use the existing
   registry and build pipeline for the market image.
   [GCP deployment and operations](operations.md).
3. **Verify on-demand access.** After authorization, publish the API and UI
   and test a signed-in dashboard load from a cold service during a regular
   session. Record startup and response latency. Verify that unauthenticated
   and non-operator requests are rejected, live data shows its freshness, and
   requests stop when the dashboard closes. Check Cloud Run metrics after idle
   and confirm the collector is still healthy. For rollback, restore the previous
   API revision and frontend build as a compatible pair, or withdraw access
   for a first release. Treat schema recovery as a separate operation.
4. **Add hosted research and paper trading in stages.** Verify one bounded
   cloud research run, retry and cancellation behavior, artifact recovery, and
   per-run cost before opening batch access. Run strategy evaluation and broker
   execution in separately authorized worker processes with durable state,
   exchange-calendar scheduling, and the platform's paper-trading gates. Size
   and price those processes from measured workloads before approval.
   [Delivery plan and acceptance gates](trading-platform-plan.md#15-delivery-plan-and-acceptance-gates).
