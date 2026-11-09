# Infrastructure development rules

Follow the repository-wide [development rules](../AGENTS.md) and
[security guidance](../docs/security.md). These rules apply to all infrastructure
under `infra/`.

## Secrets and build artifacts

- Keep secret values out of Terraform configuration, variable files, backend configuration, plans, state inputs, build arguments, images, and source uploads. Terraform may manage secret containers and grants, not secret contents/versions.
- Use existing ADC and runtime Secret Manager patterns. Do not download service-account keys or print access tokens. Pin explicit secret version identifiers where the deployment pattern requires them.
- Never commit local state, plan files, credential files, or local variable files. Do not inspect or print existing state/plan contents unless necessary; treat them as sensitive.
- Preserve `.gitignore`, `.dockerignore`, `.gcloudignore`, and the audited source allowlist. Do not widen build contexts or upload the working directory indiscriminately.

## Least privilege and deployment safety

- Scope IAM to the necessary identity, resources, and permissions. Keep runtime, build, and deployment privileges separate; do not grant broad roles to avoid diagnosing an access issue.
- Keep services, metrics, storage, and database access private unless public exposure is explicitly authorized. Preserve TLS, public-access prevention, state versioning, and destruction protections.
- Keep one root Compose file and the existing machine-wide Docker Desktop port binding preference. Do not change networking defaults or scaffold a frontend deployment without authorization.
- Do not run cloud applies, deployments, destroys, credential rotation, or other infrastructure mutations without explicit authorization. Keep the existing automation schedule unchanged.
- Treat Terraform plans as review artifacts, not approval to apply. Explain any replacement, deletion, privilege expansion, cost increase, or public exposure before requesting approval.

## Verification

- Never spend cycles formatting infrastructure code or running standalone formatting commands/checks unless explicitly requested. Follow the root formatting policy and report missing hook coverage rather than manually formatting files or adding formatters.
- Use proportionate offline/local configuration validation for infrastructure changes. Do not run remote plans or refresh state merely to verify documentation changes.
- Follow the root testing policy: do not introduce unit tests, test frameworks, or coverage workflows without an explicit request. Leave existing tests and tooling intact.
