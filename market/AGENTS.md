# Backend development rules

Follow the repository-wide [development rules](../AGENTS.md). This file applies
to all backend modules under `market/`.

## Architecture and code safety

- Never spend cycles formatting backend code or running standalone formatting commands/checks unless explicitly requested. Follow the root formatting policy and leave formatting to pre-commit.
- Keep existing data, research, risk, portfolio and simulated execution code in `market/`; the HTTP control plane lives in root `api/`. The user explicitly authorized separate workers and the local event platform on November 12, 2026; preserve the existing entry points and domain boundaries.
- Validate request, provider, and artifact data at their boundaries. Keep domain types separate from transport/SDK objects and enforce bounds on sizes, ranges, and resource consumption.
- Use SQLAlchemy mapped models and ORM sessions for application database access. No handwritten application SQL, text() wrappers, raw connections or driver COPY. Preserve transaction/locking semantics using ORM expressions. Never interpolate untrusted values into commands or file paths.
- Avoid shell execution when a library or argument-list subprocess call will do. Do not deserialize untrusted data with executable formats or evaluate user-supplied code.
- Bound external calls, retries, concurrency, and buffering. Preserve transaction boundaries, cancellation, cleanup, and idempotency; fail safely rather than partially reporting success.

## Access and observability

- Keep secrets, privileged database operations, and provider credentials server-side. Hosted runtimes must use restricted roles; the user explicitly chose the configured PostgreSQL superuser for local development only.
- The local-only API requires a runtime `BAGEL_API_KEY` through `X-API-Key` on versioned routes and metrics, with unauthenticated health probes. It must remain loopback-published. Do not add Supabase Auth or broaden CORS. Production authentication/authorization is a separate requirement before any rollout.
- Sanitize errors and logs. Do not log raw transport/database exceptions, request/response bodies, authentication headers, or connection configuration that can contain secrets.

## Research and verification

- Keep research offline with synthetic fixtures and simulated execution. Do not enable broker order submission, provider ingestion, or historical downloads without explicit authorization.
- Preserve completed-bar semantics, UTC/timezone handling, series identity, and deterministic research inputs. Do not fill missing market prices with invented data.
- Follow the root policy: no unit tests at all. Use proportionate static checks, structured container logs and temporary local smoke verification; clean up temporary artifacts without disturbing existing data.
