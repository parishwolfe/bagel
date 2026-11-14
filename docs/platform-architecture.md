# Local event platform

## Ownership and boundaries

The root Compose stack separates market, strategy, execution/account and research
processes. `api/main.py` and `api/dockerfile` remain the API entry points; domain
code stays in `market/`. `ui/` contains only operator controls and presentation.
All application tables/functions remain in `public`. The intentionally privileged
local database model must not be carried into production.

NautilusTrader supplies reference ideas—actors, deterministic events, shared
strategy interfaces, cache/read models and explicit execution ownership—not an
imported implementation of trading/research logic.

## Market data and delivery

1. Provider adapters normalize stocks/ETFs into completed UTC one-minute bars.
   Source/feed/adjustment/currency and stable instrument mapping remain explicit.
2. Bounded queues/batches write native latest-bar partitions, immutable revision
   envelopes and a journal entry in the **same** database transaction. Historical
   ingestion bounds symbol groups and pages instead of buffering a full-market day.
   History jobs and flat-file imports honor the configured database budget, stop
   without deleting existing data, and expose committed range progress.
3. A singleton publisher assigns sequence numbers only after rows are committed.
   A late-committing transaction cannot fall behind an already-advanced cursor.
   This is a visibility/checkpoint guarantee, not a claim of exchange-global order.
4. Redis messages use stable sequence-based IDs. The retained PostgreSQL journal
   reconstructs a lost/trimmed stream. Delivery and consumer acknowledgment alone
   never establish that financial effects committed.
5. The readiness consumer commits its effect and checkpoint before ACK. Strategy
   actors keep independent durable cursors and use Redis only for wakeups; their
   database replay works when Redis is lost or unavailable.

Envelopes include provider event time, receipt/observation/availability times,
schema version, origin, payload checksum and immutable revision identity. Historical
bars received today were available to this system today—not at their original
market timestamp. Latest-bar precedence is historical correction, updated live
bar, initial live bar, then observation time within that class. Empty minutes are
not filled with invented prices.

## Strategy actors

`market/strategies/runtime.py` is provider-free and used by both historical replay
and live actors. The registry supports per-instrument and cross-sectional logic.
Checkpoints contain configuration/version and rolling state. Universe membership
is frozen from explicit symbols or all ingested members of a selected feed; it is
not a hardcoded universe or an implicit subscription request.

An actor holds a token/lease and row lock while atomically advancing its input
cursor, selecting exact revision inputs, checkpointing, recording signals and
enqueuing attributed target commands. Interrupted transactions roll back all of
those effects. Replacement claims preserve committed state; paused/changed-engine
runs are not silently restarted under incompatible code.

Frames close after the completed-minute boundary plus configured delay, once the
committed input journal is caught up. Missing-member policies either skip the
frame or use contemporaneous available members above the explicit coverage floor.
No stale member is carried forward into a cross-sectional rank. Late revisions
are counted/archived without retroactive live orders. Stale recovery frames can
warm/reconstruct state but cannot produce executable expired decisions. Frame
buffer and rolling-state budgets bound resource use independently of risk exposure.
Warmup and accepted-frame revision references remain in `strategy_inputs`.
Signals are immutable and activation-specific. Paper commands carry their signal
UUID, not a duplicate cross-sectional revision vector. Admission resolves the
selected member's normalized provenance and links the signal in the order/risk
audit. Legacy signals retain unknown activation metadata and cannot authorize new
normalized targets.

`strategy.rebalance_policy` defaults to `on_weight_change`: retain whole-share
size until weight changes. Explicit `every_frame` recalculates from each frame.
Replay clears sizing on splits. Paper persists accepted/satisfied sizing per
account/strategy/symbol/activation, using its account equity snapshot and signal
price; this shares sizing arithmetic, not broker-versus-simulator fill guarantees.
New frames supersede queued targets with frame-bounded work; admission/dispatch
also fence stale signals. Zero targets are omitted only without attributed,
pending or cached nonzero exposure. Pending targets wait for terminal reservation
release or expire. Reversals close before opening the other side. Supersession
never cancels a broker order or releases an ambiguous submission's reservation.

## Account, risk and execution

Execution owns one broker account and a fenced token. Startup/replacement pauses
submission. Trading updates invalidate readiness and update order evidence;
canonical REST account activities are the only source of booked financial fills.
This avoids double-booking REST/stream duplicates. Cash, long/short weighted basis,
reversals, realized P&L, fees and strategy/external attribution use decimals.

Reconciliation checks account identity, broker clock, all open orders, in-flight
client IDs, activity quantities, cash and positions. Unknown orders, unsupported
activities, missing fills, disconnected streams or inconsistent evidence block
new submission. Normal activity scans overlap seven days; the operator can request
a paused full-history scan. Late canonical activities replay in bounded memory,
retaining original source/audit records and appending reconciliation adjustments.

Admission locks the account row across all strategies. It validates session,
entitlement/asset/borrow capability, decision/revision availability, account/feed/
mark freshness, existing exposures, policy loss/drawdown limits, buying power,
position/gross/net/short limits and margin. Accepted intents reserve worst-case
buffered exposure, including conservative grouped-order allowance. Held marks
require local completed-bar evidence; broker snapshot time is not presented as a
market-price timestamp. Dispatch rechecks current risk and records that evidence.

Dispatch commits its unique client ID and one-attempt state **before** its single
HTTP POST. There is no SDK POST retry, redirect following or implicit mutation
permission override. Ambiguous outcomes remain reserved, block further dispatch,
and reconcile by the same client ID. 404 does not permit another POST. A remote
broker call already in progress cannot be revoked by a local pause or lease loss;
restart never causes a second attempt of that intent.

Terminal reservation release waits for booked fills and terminal evidence for
the expected grouped legs. Cancellation remains available while paused and does
not itself release exposure. Pause/cancel controls are processed before a failing
reconciliation, so read failures do not make the pause command unreachable.
Manual operator orders may close an attributed position after its strategy is
paused; strategy-generated targets cannot continue after pause.

## Research

Dataset v2 pins Parquet and exact-revision-sidecar checksums. Legacy v1 identities
remain stable and legacy unarchived rows are explicitly counted. Exports use a
repeatable-read local snapshot. Revised-history availability is not a point-in-time
provider-publication archive.

Experiments incrementally generate bounded trials into the existing fenced queue.
Generation itself is creation-engine-pinned; unknown legacy engines are not guessed.
Workers run in a bounded process pool, renew claims, observe cancellation at frame
checkpoints, stream fill logs to immutable artifacts, and commit completion only
while still owning the claim. Shared normalized runs remain shared on experiment
cancellation. Dataset/configuration/code/dependency fingerprints identify runs;
incompatible queued work requires its original engine or a newly submitted run.
Newly queued engines archive the explicitly selected algorithm/reader source files
and dependency metadata in an immutable checksummed record. The API can export
that evidence but never executes uploaded/archived code. This is not a complete
OS/container/environment snapshot; restoring an old environment remains an
operator-reviewed task. Runs created before source archiving remain explicitly legacy.

Replay never imports or calls a provider adapter. Signals use completed closes;
execution uses a later eligible bar open after explicit availability delay. Fill
models, participation, fees, slippage and borrow scenarios are research assumptions,
not broker-fill guarantees. Corporate-action research events are explicit inputs.

## Operational limits

This is not production HA. There is no validated large-AUM liquidity/impact model,
complete corporate-action accounting, real full-market subscription proof, hosted
role/auth deployment, backup/restore drill or unattended multi-day soak result.
Unsupported account events fail closed. No automatic journal/revision retention
is configured. Legacy free-tier watchlist capacity-fallback code remains separate;
Compose explicitly disables automatic backfill. Use durable jobs for the new local
platform, not destructive free-tier retention as a research-data policy.
See [acceptance evidence](platform-verification.md) before making trading claims.
