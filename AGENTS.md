# Bagel project memory

- The platform is in local research and validation. Use Docker and the local Supabase stack for development. Follow the [local startup instructions](README.md#local-supabase-stack), run the relevant smoke checks after implementation, and leave the local stack running for review.
- Local ingestion defaults to periodic historical polling. Enable the live stream only when the Alpaca account has a connection slot available.
- Use the existing machine-wide Docker Desktop port binding preference for Bagel's local stack.
- Keep one root `compose.yaml`. Use `market/` for backend modules and reserve `ui/` for the frontend. Wait for user authorization to scaffold the UI. Plan separate Terraform deployments for the frontend and backend.
- Production rollout is on hold pending local strategy tests, reproducible backtests, and explicit user authorization. The hosted Supabase GitHub integration makes a merge to `main` a production release path for migrations. Apply the same hold to hosted database changes, cloud releases, and broker deployments.
- As of November 9, 2026, the user reports that the existing automation runs daily and costs are relatively low. Keep its current schedule during local development.
- The [trading platform plan](docs/trading-platform-plan.md) is the source for scope and milestones. The MVP covers long and short US stocks/ETFs on completed one-minute bars or slower. The planned UI uses Next.js, MUI, and Zustand with a FastAPI control API.
