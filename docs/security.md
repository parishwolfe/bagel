# Credentials and access

Never commit `.env`, database URLs/passwords, credential JSON, private keys,
Terraform state, plans, or local variable files. `.gitignore`, `.dockerignore`,
and the restrictive `.gcloudignore` protect their respective paths. Cloud Build
receives an audited runtime source allowlist, not the whole working directory.
No credentials are embedded in the image, Docker build arguments, or Terraform.

GCP Secret Manager stores only these application secrets:

| Secret | Injected environment variable |
|---|---|
| `bagel-alpaca-api-key` | `ALPACA_API_KEY` |
| `bagel-alpaca-secret-key` | `ALPACA_SECRET_KEY` |
| `bagel-database-url` | `DATABASE_URL` |

Secret values are submitted through stdin, never command-line arguments.
Terraform manages the containers and access grants, not secret versions/contents.
Deployments pin explicit version identifiers. The local Supabase administrator
password and publishable API key are not uploaded. `.env` is preserved unchanged.

`bagel_gcp_worker` is a dedicated Supabase LOGIN inheriting `bagel_ingester`, with
no superuser, create-role, create-database, replication, or RLS-bypass capability.
Its generated random password is represented by a SCRAM verifier when setting the
PostgreSQL password. GCP uses the IPv4 Supavisor **session** pooler on port 5432,
TLS required; session affinity is necessary for the advisory lock. The worker
cannot access the private migration ledger or arbitrary DDL. Security-definer
maintenance functions have empty search paths and narrowly granted execution.

`bagel-market-data` is the runtime Google service account. It can access only the
three named secrets and has no state-bucket or deployment grant. `bagel-build` can
read the private source bucket, write the Bagel Artifact Registry repository, and
write build logs. Neither account has downloaded service-account keys. Deployment
uses the signed-in gcloud user and local Application Default Credentials (ADC).
ADC's quota project is explicitly set to Bagel; never print an access token.

The GCS state bucket enforces public-access prevention and uniform bucket access,
has object versioning, and Terraform prevents its destruction. Backend credentials
are supplied through ADC, not `backend.hcl`. State contains secret identifiers,
not values. Source uploads expire after seven days; state history is retained.

JSON logs deliberately omit raw transport/database exceptions, authentication
payloads, response bodies, and configuration values. Application metrics have
private container endpoints, with no public Cloud Run service endpoint.

To rotate credentials, stop the old worker first, run the provisioning tool, pin
the returned version numbers, deploy, and verify before disabling old versions.
The tool rotates the database password; do not run it casually against an active
worker. Revoke unused old versions after successful rollout. For a compromised
Alpaca credential, rotate it at Alpaca as well as in GCP.
