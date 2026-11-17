# Lox

An OpenCode **parent → phase-subagent** research pipeline. Strategy code lives in
`src/bagel/strategies/` and uses Nautilus directly. Lox owns the small offline
sweep runner and research bookkeeping; it never imports Bagel's application,
loads `.env`, connects to IBKR, places live orders, or runs Git.

## Workflow

The `lox` primary agent follows one procedure:

```
claim topic
  → spawn lox-research → brief.md
  → spawn lox-code     → src/bagel/strategies/<slug>.py + smoke runs
  → spawn lox-review   → independent logic review
  → spawn lox-backtest → parallel training grid + held-out evaluation
  → spawn lox-analyze  → report.md
  → record outcome → compact → next topic
```

The parent waits for each actual subagent completion notification. Children have
fresh context and receive explicit artifact paths. `lox next` and `lox advance`
enforce the order and artifact gates. Review/implementation failures get at most
two repair rounds, then a recorded failure. Three consecutive failed topics stop
new claims. Missing data/capability is **blocked**, not nonviable.

`lox-scout` is a separate parent: spawn `lox-discover`, deduplicate the returned
ideas against prior work, then append new topics. Parameter variations remain
within a strategy's grid instead of becoming duplicate backlog items.

## Four readable files

- `research/backlog.md`: prioritized topics, intraday/scalping first.
- `research/researched.md`: completed, blocked and failed topics with evidence.
- `research/candidates.md`: reviewed results exceeding **36% annualized on validation**.
- `research/data-requests.md`: missing data or runner capabilities and why needed.

These files contain research bookkeeping, **not strategy parameter definitions**.
The CLI locks and atomically replaces them; interrupted recording is retryable.
Per-topic Markdown artifacts, phase checkpoints, fingerprints and result CSVs go
under `research/topics/<slug>/` (ignored by Git). Nothing automatically deletes
research results. Keep that directory if moving an in-progress investigation.

## Install on the research machine

Requires this checkout, Python 3.14 through uv, OpenCode **2.3+**, and a local
Nautilus catalog. Run from the checkout root:

```bash
sh src/lox/install.sh --dry-run
sh src/lox/install.sh
uv run --no-sync lox inventory
```

The installer syncs the locked Python dependencies and copies only the Lox
Markdown agents/commands into the checkout's `.opencode/`. It backs up changed
Lox definitions, is idempotent, and does not touch global settings, provider
credentials, other agents, or existing OpenCode JSON/JSONC configuration.
`--dry-run` does not sync or write anything. To preview another checkout, add
`--target /path/to/checkout`.

Choose your Ollama or AirLLM-server model/provider in OpenCode as usual. Agents
inherit the parent model unless you set their native `model` frontmatter or
`agents.<id>.model` override. Model routing/Jev is yours to configure. No nested
subagent-depth setting is needed: each parent directly spawns its phase children.
MCP wiring is deliberately left to you; allow only the specific research tool
actions on `lox-research`/`lox-discover` when adding servers. Built-in web search
works now if configured; lack of search is reported, not filled with invented sources.

In OpenCode/OpenChamber:

```
/lox-one orb
/lox-run
/lox-scout
/lox-status
```

`/lox-run` processes at most ten topics per invocation and requests native
compaction between them. For an ongoing run, the optional shell loop starts a
fresh parent session per topic, avoiding accumulated context:

```bash
sh src/lox/run.sh
```

It stops on an empty backlog, `research/STOP`, a failed OpenCode command, or an
invocation that records no progress. It never uses `--auto`.

## Strategy contract

Lox ships **no strategy implementation**. ORB is the first backlog topic:
`/lox-one orb` must research it and create `src/bagel/strategies/orb.py`.
The coding subagent creates a module exporting:

- `STRATEGY`: its native Nautilus strategy class.
- `TIMEFRAME`: `"1min"` or `"1day"`.
- `SYMBOLS`: a tuple of catalog ETF symbols, initially SPY/QQQ.
- `parameters()`: a Python generator of constructor keyword-argument dictionaries,
  each including a `variation` name. Ranges and combinations are defined in code.
- `FLAT_AT_END`: optional boolean assertion for an intraday strategy.

The strategy subclasses `nautilus_trader.trading.Strategy` and accepts
`instrument_id`, `bar_type`, and the generated keyword arguments. Use native
Nautilus indicators, orders and portfolio. No Lox base class or Bagel imports.
The pinned PyO3 Strategy requires a `__new__` override that absorbs custom
constructor arguments; the existing `src/bagel/strategies/base.py` illustrates
that Python binding detail. Keep the generated strategy self-contained.
The loader supports pure Python calculation/time utilities and native Nautilus
model, indicator and strategy imports. It is a mistake filter, not a Python sandbox.

The grid must generate at least 50 unique parameter dictionaries and three
meaningful variations. All constructors are checked, then one configuration per
variation/symbol gets a one-month smoke run. The research and coding subagents
choose the variations and ranges for each hypothesis. The backtesting phase
expands that generator across symbols and optimizes the configurations.

## Backtests and interpretation

`src/lox/settings.toml` contains **operational settings only**: catalog path,
capital, parallelism, limits, fees, date windows and the candidate threshold.
The existing Bagel commands/configuration remain usable.

- Native Nautilus engine; single-instrument, long-only **cash equities** for now.
  Pairs stays in the backlog, and gets a capability request until the runner
  supports simultaneous instruments. No synthetic single-leg pairs results.
- $50,000 account; zero base commission; one-tick slippage. Fee stress uses
  $1/order by default (not “double zero”). Strategies size from account equity.
- Orders have positive simulated latency, so a completed-bar signal cannot
  receive a fill from that bar's already-observed OHLC. Minute bars still do not
  model queue position, bid/ask spreads, real tick liquidity or market impact.
- Training chooses three finalists, then validation evaluates those unchanged.
  No grid retuning on validation. Data beyond validation end is the untouched
  vault; catalog queries are window-bounded rather than loading then slicing it.
- Return is marked account equity after fills and fees; annualization uses the
  actual calendar span. Drawdown is **daily-close**, not intraday drawdown.
  Buy-and-hold is a before-cost price-return reference, not dividend total return.
- Candidate flagging follows the >36% request. Trade count, benchmark,
  fee stress and profitable training neighbours are shown as diagnostics.
  Here neighbours mean grid alternatives differing in one parameter, not a
  statistical proof of local stability.
  Multiple testing and a short holdout can still produce false discoveries.
  Candidates require further review/paper testing; Lox has no execution path.

The research/coding/review phases must establish session rules, half-day handling,
stop/target execution and warmup for each generated strategy. IB minute bars in
this catalog are **end-stamped**.

## Run control and recovery

Worker count defaults to two, capped at eight and CPUs minus two (minimum one).
Each trial is a fresh subprocess with a wall timeout and CPU limit. Linux also
enforces the configured address-space limit. Use an OS/container memory limit on
macOS; `RLIMIT_AS` is not reliable there. A disk floor and grid cap bound work.
OpenCode permissions allow only phase-specific files and CLI operations.
For unattended generated Python, use the requested dedicated non-admin account
with read-only catalog access and no broker credentials.

Create `research/STOP` to stop between phases/batches; the current batch can take
up to its worker timeout. Interrupt the parent session to stop model work.
After the old parent is stopped, recover an interrupted topic with:

```bash
uv run --no-sync lox resume orb
```

Then `/lox-one orb` in the new parent session. Remove STOP manually when ready.
After the three-failure stop, inspect the failures and use `lox resume <pending-slug>`
to explicitly release the next pending topic to a new parent.
Do not run two parents over the same research directory. Training resumes from
completed CSV rows. A changed strategy, runner or settings creates a different
fingerprint and requires a fresh smoke check and review. Catalog file sizes and
modification times also enter the fingerprint, so replacing data invalidates
cached results. Validation reruns as a
whole after interruption. To revisit a completed/blocked topic, add a distinct
follow-up slug with its changed hypothesis/data and reference the original.

## Manual verification

No unit tests. Once the pipeline has generated a strategy, use `lox check` and
its backtesting phase. The implementation can also be checked with CLI recovery
exercises, scoped Ruff checks and an isolated installer dry-run.
See `VERIFICATION.md` for what was actually run during implementation.

OpenCode contracts checked against the current primary documentation:
[agents](https://opencode.ai/docs/agents),
[permissions](https://opencode.ai/docs/permissions),
[commands](https://opencode.ai/docs/commands),
[compaction](https://opencode.ai/docs/compaction).
