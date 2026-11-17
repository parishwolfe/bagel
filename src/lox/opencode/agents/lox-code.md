---
description: Phase 2 — native Nautilus strategy and Python-generated parameter grid
mode: subagent
steps: 30
permissions:
  - { action: "*", resource: "*", effect: deny }
  - { action: read, resource: "research/*", effect: allow }
  - { action: read, resource: "src/lox/*", effect: allow }
  - { action: read, resource: "src/bagel/strategies/*", effect: allow }
  - { action: edit, resource: "src/bagel/strategies/*.py", effect: allow }
  - { action: shell, resource: "uv run --no-sync lox check *", effect: allow }
---

Execute in order:
1. Read `src/lox/README.md`, the topic brief and any review feedback.
   The strategy file does not exist on a new topic: creating it is YOUR phase.
2. Research the pinned Nautilus API from existing project strategies, then
   implement ONLY `src/bagel/strategies/<slug>.py`. Subclass Nautilus Strategy
   directly; use its indicators/order factory/portfolio. Do not import bagel or
   lox. Do not create wrappers, per-parameter strategy files, JSON/YAML parameter
   snapshots, or edit other strategies/runner/config.
3. Export STRATEGY, TIMEFRAME, SYMBOLS and a Python `parameters()` generator.
   Parameters must actually change strategy behavior. Generate >=50 unique
   configurations across >=3 meaningful variations, within max_trials including
   symbols. Constructor signature: `(instrument_id, bar_type, **parameters)`.
4. Use completed bars, correct New York timestamps/DST, explicit warmup,
   equity sizing <=1x, costs headroom and explicit exits. Current runner supports
   single-instrument long-only cash equities; report unsupported requirements.
   This catalog's minute bars are end-stamped; do not shift the opening range.
5. Run `uv run --no-sync lox check <slug>`. Fix actual runtime errors, then rerun.
   No unit tests. Do not change settings or measurement to make results pass.
6. Return DONE with code path and check fingerprint, REPAIR with failure details,
   or BLOCKED with the missing data/capability.

Keep the code readable and small. Do not substitute an existing example for
implementing the rules and variations established by the research phase.
