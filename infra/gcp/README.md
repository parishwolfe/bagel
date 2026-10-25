# GCP infrastructure

Bagel uses Terraform with a private GCS backend, Artifact Registry, Cloud Build,
Secret Manager, a Cloud Run worker pool, and two Cloud Run Jobs in `us-east4`.
See [deployment and operations](../../docs/operations.md) for the deployed inventory,
release commands, authentication, remote state, monitoring, and rollback.

`backend.hcl` and `production.tfvars.example` contain only nonsecret resource IDs,
image digest and secret version identifiers. Actual secret values are provisioned
outside Terraform with `tools/provision_gcp_secrets.py`. Never add secret values
to Terraform variables, plans, state, source uploads, or GitHub.

Offline CI checks:

```sh
terraform init -backend=false -input=false
terraform fmt -check
terraform validate
```

Deployment uses the configured GCS backend and the production variables; do not
reuse an offline CI initialization for an apply. Defaults deliberately leave the
runtime disabled for initial provisioning.
