# Backend development rules

Follow the repository-wide [development rules](../AGENTS.md). This file applies
to all backend modules under `market/`.

## Architecture and code safety

- Never spend cycles formatting backend code or running standalone formatting commands/checks unless explicitly requested. Follow the root formatting policy and leave formatting to pre-commit.
- Keep existing data, research, risk, portfolio and simulated execution code in `market/`; the HTTP control plane lives in root `api/`. Follow [the backend boundaries](README.md). Do not implement the future multi-node split or add parallel services without separate authorization.
- Validate request, provider, and artifact data at their boundaries. Keep domain types separate from transport/SDK objects and enforce bounds on sizes, ranges, and resource consumption.
- Parameterize SQL values and use driver-supported identifier composition for dynamic identifiers. Never interpolate untrusted values into SQL, shell commands, or file paths.
- Avoid shell execution when a library or argument-list subprocess call will do. Do not deserialize untrusted data with executable formats or evaluate user-supplied code.
- Bound external calls, retries, concurrency, and buffering. Preserve transaction boundaries, cancellation, cleanup, and idempotency; fail safely rather than partially reporting success.

## Access and observability

- Keep secrets, privileged database operations, and provider credentials server-side. Hosted runtimes must use restricted roles; the user explicitly chose the configured PostgreSQL superuser for local development only.
- The local-only API requires a runtime `BAGEL_API_KEY` through `X-API-Key` on versioned routes and metrics, with unauthenticated health probes. It must remain loopback-published. Do not add Supabase Auth or broaden CORS. Production authentication/authorization is a separate requirement before any rollout.
- Sanitize errors and logs. Do not log raw transport/database exceptions, request/response bodies, authentication headers, or connection configuration that can contain secrets.

## Research and verification

- Keep research offline with synthetic fixtures and simulated execution. Do not enable broker order submission, provider ingestion, or historical downloads without explicit authorization.
- Preserve completed-bar semantics, UTC/timezone handling, series identity, and deterministic research inputs. Do not fill missing market prices with invented data.
- Follow the root testing policy: no unit-test creation or maintenance unless explicitly requested. Use proportionate static checks and temporary local smoke verification instead; clean up your temporary artifacts without disturbing existing data.
