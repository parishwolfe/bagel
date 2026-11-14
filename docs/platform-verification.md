# Platform verification

Local validation on `dev`, November 13, 2026. Changes are uncommitted. Nothing has
been pushed, merged, deployed, applied to hosted Supabase, or used to change the
existing hosted automation schedule. No secret environment files were edited by
the agent.

**This is not a production or $25M-AUM reliability sign-off.** Fixture evidence,
real-provider evidence and remaining integration gates are distinguished below.

## Verified foundation

- Root Compose starts PostgreSQL/TimescaleDB, Redis, migration, API, publisher,
  readiness consumer, market/Massive workers, strategy, execution, research and UI.
- Existing PostgreSQL 17 data volume and historical DDL were preserved. The local
  migration runner applied additive files through `0021`; fresh disposable
  databases and repeated checksum-verified migration runs passed.
- API retains `api.main:app`, the API Dockerfile and hot reload. Worker source
  changes require restart. All application objects remain in `public`.
- API/UI are loopback-published. Versioned routes/metrics reject missing keys;
  probes remain unauthenticated. Research/strategy processes have no broker keys.
- Next.js production build and TypeScript compilation passed. The authenticated
  proxy rejects foreign-origin mutations. A standalone-host origin mismatch was
  found and corrected using the request authority while retaining origin checks.
  No browser automation/visual interaction check has been performed.
- Final images were rebuilt and the stack recreated with submission explicitly
  disabled. All 11 running services were healthy; all seven configured worker
  roles had live, ready heartbeats. Older stopped actor-heartbeat rows are retained.
  A checker initially counted those rows as active; the corrected check validates
  live configured roles. Auth/origin/proxy/metrics checks passed again.
- Final signal-only run `5b15172d-5abd-4722-b5d2-16dc8a819472` passed create,
  idempotent start, four-member local warmup and pause. This is a control-plane
  check, not regular-session automated trading. The account remains flat/paused,
  with no reservations, and the final checks created no real orders.

## Real Alpaca evidence

### Market data

- Actual IEX WebSocket authentication/subscription and completed intraday bars
  were received through the normal collector, revision archive and journal path.
  The configured collector example was SPY/QQQ/IWM/VXUS, not a strategy restriction.
- Actual REST history was persisted, including the previously verified SPY/QQQ
  window `2026-11-12T15:00:00Z`–`16:00:00Z` (120 rows).
- A controlled collector outage left `2026-11-13T16:52:00Z`–`16:53:00Z` missing.
  Automatic REST repair received both SPY/QQQ bars at `17:08:39.390394Z`, respecting
  Basic's recent-history restriction. Stored OHLCV subsequently matched a fresh
  actual REST read. Neither bar had an initial live revision from that outage.
- The original outage checker incorrectly looked up `alpaca:iex`; the actual
  stream identity also includes its resolved symbols. That checker failed despite
  successful repair. Corrected identity inspection and a direct REST comparison
  established the evidence above; no collector workaround was added.
- Updated-bar normalization and wildcard batches have fixture coverage. Actual
  full-market wildcard entitlement and real wildcard capacity are **not verified**.
  Basic IEX is real-time; the REST restriction does not mean the IEX stream is delayed.

### Paper account and orders

Actual account reads and authenticated trading-update streaming passed. Run
`7e818d40-5ca6-4072-ae81-d5aeee1b47ea` performed:

1. One limit order, lookup and scoped cancellation.
2. One-share SPY market buy and market sell, with two canonical confirmed fills.
3. Three terminal intents, three dispatch attempts, zero remaining reservations,
   reconciled cash/positions and a flat account.

The earlier pagination-check failure retained its reservation and was resolved
without a second order POST. The current Paper service can repeat the cursor
order on a short final page; this is tolerated. A repeated full page still blocks
readiness. Read-only pagination over distinct existing orders was also checked.
Pause/cancel controls now run before reconciliation, so a read failure does not
make pause unreachable.

Runtime submission was restored to **false** and the account left **paused**.
No cancel-all or account-wide liquidation was used. Real tests of every stop,
trailing, grouped, fractional and short-order combination remain outstanding.
No real strategy-to-Paper automated trading run has been completed; after market
hours, further regular-session trading checks were explicitly deferred.

## Isolated, provider-free evidence

Disposable uniquely named local databases and run-owned Redis streams were used.
Temporary verifier scripts were removed after collecting results; existing tests
were not expanded during this continuation.

### Financial and recovery gates

- Fill deduplication, partial fills, weighted basis, cash and long/short reversal
  arithmetic; stream evidence does not book a second copy of a canonical REST fill.
- Late canonical-activity replay, immutable original ledger/source records, and
  audited reconciliation adjustments.
- Late broker ownership discovery rebuilds strategy/operator attribution without
  rewriting original source ledger entries.
- Concurrent strategy admission shares the account lock and reservations; a pair
  of individually acceptable orders cannot bypass their combined position limit.
- Grouped partial-fill reservations remain held until parent/expected children
  are terminal and canonical fills have been booked.
- Timeout/HTTP-504/malformed-acknowledgment ambiguity produces one outbound POST.
  A 404 lookup never permits an automatic retry; replacement ownership retains
  the unresolved reservation. Stale account owners cannot commit.
- Eleven additional admission checks covered stale/invalid held marks, existing
  over-limit positions, candidate-versus-held valuation, future/uncompleted input,
  blocked state, borrow/short permission, loss/drawdown gates and genuine reduction.
- Runtime-disabled transports do not POST. Startup/replacement does not arm a run.

These are deterministic fixtures, not evidence of actual broker liquidity,
production failover, margin approval or an unattended trading soak.

### Strategies and research

- Per-instrument and cross-sectional runtime parity, restored rolling checkpoints,
  transactional crash rollback/replay, lease replacement, missing-frame policy,
  late-revision policy and exact warmup/frame provenance passed.
- Attributed strategy targets flowed through the real account admission/dispatch
  implementation using fixture transport, and confirmed fixture fills reached the
  correct strategy's virtual portfolio.
- Shared whole-share sizing passed for `on_weight_change` (the preserved replay
  default) and explicit `every_frame`, including split resets and exact fractional
  weight checkpoints. Paper caches accepted/satisfied sizing per activation; its
  equity snapshot and broker fills are not claimed to match simulated execution.
- Normalized targets link immutable, activation-specific signals. A 16-member
  frame emitted one 447-byte target, with the full revision vector stored once in
  the signal rather than copied into each command. Signal/member/decision/price/
  weight/policy checks, stale-activation rejection and restricted-role reads passed.
- Both queued-target supersession race orderings passed, including first-cache
  insertion. Held/pending zero targets survive filtering; empty zero targets do
  not enqueue orders. Pending targets wait for reservation release or expiry.
  Reversals close before opening the other side. Dispatch fences superseded
  signals and never cancels or forgets an already attempted ambiguous order.
- Replacement ownership preserves sizing but pauses submission. Canonical fixture
  fills retained their signal-linked strategy attribution and deduplicated ledger
  effects. A restricted strategy actor completed a frame; signal UPDATE is denied.
  This does not constitute a complete production-role/security audit.
- Dataset v1 identity compatibility is preserved; v2 exports pin Parquet and exact
  immutable revision sidecars. Legacy unarchived rows remain explicitly counted.
- An 18-trial frozen real-history sweep passed with 17 successes and one deliberately
  invalid configuration. Idempotency, pagination, archived-source checksums,
  downloadable fill logs and bit-identical offline replay passed. Dataset:
  `0c702fd7413f5bc577092976e739f05aa6295ea37dfaa088f12f7686475511eb`;
  experiment `2aeb2b44-9e62-4714-a131-65e5c33ce695`;
  replay `ff37df5f-3028-4306-8260-74dc03f4c380` (before the final sizing changes).
- After final sizing/source changes and worker recreation, experiment
  `357d1d41-99d1-4e6a-8fd8-3a4c899b6ff2` again completed 17 successes/one deliberate
  invalid configuration under its pinned creation engine. Run
  `8aa8893c-a579-4a18-8baa-86d0597e2f5f` replayed bit-identically; source, result and
  fill-log checksums/downloads passed. Final engine:
  `221d7d73706816d4cdd2cd8aaf6518e7b4aa7733e38daf153c4bee4dcf0cb391`.
- Experiment generation now pins its creation engine as well as individual runs.
  A changed engine cannot generate remaining trials. Legacy creation-engine values
  remain unknown, rather than being inferred from generated children.
- The mixed-load experiment below completed 448 synthetic trials before scoped
  cleanup canceled its 32 remaining run-owned queued trials. A completed run's
  streamed fill-log checksum/count matched its summary (1,615 fill records).
- Massive REST pagination-origin/calendar/normalization fixtures and a checksummed
  stock-minute CSV/gzip import passed. The import used the canonical writer/archive
  path in an isolated database; malformed format/checksum gates rejected inputs.
  This does **not** satisfy the real Massive integration gate.

## Real Massive evidence and free-tier boundaries

- The normal worker authenticated and imported **60 SPY one-minute bars** for
  `2026-11-12T15:00:00Z`–`16:00:00Z`; all 60 have immutable revision references.
  Job: `823334e8-2acc-421f-85ce-e582eef822eb`. The configured rate is five requests
  per minute; isolated verification confirmed its cooldown survives job transport
  recreation. No paid access or upgrade was attempted.
- API masking covers the supplied S3 configuration/credential names; absence of
  provider credentials in the control-plane container was checked without values.
- Stocks Basic documents end-of-day REST aggregates and two years of history.
  Stock-minute S3 flat files are not included. Downloading remains disabled; a
  separate S3 secret access key is also required and was not in the supplied list.
  CSV/gzip parsing/import fixtures are not evidence of actual S3 entitlement.
- Official references: [REST aggregates](https://massive.com/docs/rest/stocks/aggregates/custom-bars),
  [flat-file quickstart](https://massive.com/docs/flat-files/quickstart),
  [stock-minute flat files](https://massive.com/docs/flat-files/stocks/minute-aggregates).
- The already imported window was frozen without provider calls as v2 dataset
  `a5ae35589b483d544f1597c214fecfb291e5d150960da0d1c7636a53e0ac138a` (60 rows,
  zero unarchived references). Batch `9498475e-1460-4d74-9301-e3119dadb217`
  completed both sizing policies; both results/fill logs replayed bit-identically
  under the final engine. Research did not request provider data.

## Capacity and recovery measurements

Docker Desktop reported 16 CPUs and 8,213,991,424 bytes of VM memory. Compose
resource limits, rather than VM CPU count alone, bound each service. These are
local synthetic measurements, not exchange throughput or broker latency claims.

| Metric | Initial ingestion/recovery | Mixed ingestion + strategy + research |
|---|---:|---:|
| Instruments | 4,000 | 4,000 |
| Measured bars | 80,000 | 80,000 + 4,000 warmup seed rows |
| Ingest seconds | 6.819 | 7.777 |
| Ingest bars/sec | 11,733 | 10,286 |
| Peak bounded queue | 10,000 | 10,000 |
| Receipt-to-commit p99 | 0.997s | 1.137s |
| Peak runner RSS | 176.85 MiB | 273.08 MiB |
| Database growth | 257,043,292 bytes | 334,236,508 bytes |
| Publication | 7.676s | 5.571s |
| Readiness consumption | 0.623s | 0.613s |
| Redis reconstruction | 4.856s | 5.471s |
| Strategy actors | None | SMA + cross-sectional momentum |
| Full-universe strategy frames | None | 40 |
| Ingest + strategy catchup | Not measured | 59.794s |

Mixed window: `2026-11-13T16:38:05.910377Z`–`16:39:21.489806Z`.
Two research processes completed **148 trials within that exact window**. Each
trial used a 36,000-row, two-instrument synthetic dataset, not a 4,000-instrument
research dataset. Live four-symbol IEX collection was also running independently.
Experiment: `2fb492f8-4919-42a5-a276-9c8856b5fb13`.

### 8,000-instrument runs

Both runs used 160,000 measured bars plus 8,000 warmup seeds, a 10,000-item queue,
two strategy actors and 40 full-universe frames. Neither included Paper orders.

| Metric | Mixed with research | Repeat after unchanged-checkpoint-write avoidance |
|---|---:|---:|
| Ingest bars/sec | 9,366.92 | 9,301.76 |
| Ingest seconds | 17.081 | 17.201 |
| Receipt-to-commit p99 | 1.401s | 1.420s |
| Peak runner RSS | 403.25 MiB | 467.41 MiB |
| Database growth | 1,016,520,704 bytes | 994,566,144 bytes |
| Ingest + strategy catchup | 128.574s | 123.756s |
| Research active | Yes | No |

First window: `2026-11-13T22:11:22.628081Z`–`22:14:03.507999Z`.
Experiment `137d5aad-c524-4dce-a669-c0cae3455b01` completed **312 trials within
that window**, and all **480 trials** ultimately succeeded. Each trial used a
36,000-row, two-instrument dataset, not an 8,000-instrument research dataset.

Repeat window: `2026-11-13T22:18:19.872205Z`–`22:20:58.987553Z`.
Research had finished, so these are **not a controlled performance comparison**.
Checkpoint hash/recovery assertions passed. A final-code 128-instrument,
640-bar/128-seed smoke also passed publication, consumer replacement, stream
reconstruction and checkpoint checks after migration `0021`.

Commit-order visibility, consumer replacement, post-XADD/pre-mark recovery, stream
loss/reconstruction and no repeated consumer effects passed. The replay is a
burst with bounded buffering; it is not a multi-day steady-state/soak measurement.
The measured growth includes indexes, archive, journal, strategy provenance and initial
allocation overhead. It must not be extrapolated into an unqualified full-history
storage promise. Full-market multi-year history requires substantial capacity.

Reproduce the isolated runner using the command in the root README. Its JSON
report states workload dimensions, provider-call count, resource measurements and
recovery assertions. Synthetic checks never substitute for provider gates.

## Static checks and limitations

- Pyright: zero errors/warnings.
- Existing unit checks: 15 passed. An upstream exchange-calendars/pandas deprecation
  warning remains visible; it was not suppressed.
- Compose configuration validation passed without printing resolved configuration.
- Ruff automatic fixes and installed formatting hooks were used. **Ruff is not
  clean:** 32 E501 diagnostics remain on SQL strings embedded in Python. SQL files
  are excluded. The user instructed that SQL files/string wrapping be left unchanged;
  no suppression, hook reconfiguration or manual SQL-formatting pass was applied.

## Remaining acceptance gates

- Real full-market Alpaca entitlement/wildcard streaming and sustained capacity.
- Broader Massive history/correction coverage and actual S3 stock-minute access
  after suitable entitlement and complete credentials; small REST access passed.
- Real regular-session strategy-to-Paper operation, short/borrow behavior, all
  grouped/advanced order states, partial fills and an unattended recovery soak.
- Large-universe research throughput, longer stress runs, host/container recovery,
  disk-exhaustion behavior and backup/restore drills.
- Complete corporate-action/account-event accounting. Unsupported events fail
  closed and require review; that is not a claim of complete broker coverage.
- Production authorization, restricted roles, security/network hardening, HA,
  liquidity/impact validation and any $25M-AUM reliability claim.

No production rollout is authorized by this evidence. Legacy free-tier retention
fallback is not a policy for pinned research data; Compose keeps automatic backfill
off. Journal/revision pruning, hosted changes and releases require separate approval.
