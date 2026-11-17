---
description: Phase 4 — bounded training sweeps and untouched held-out evaluation
mode: subagent
steps: 8
permissions:
  - { action: "*", resource: "*", effect: deny }
  - { action: read, resource: "research/*", effect: allow }
  - { action: shell, resource: "uv run --no-sync lox sweep *", effect: allow }
  - { action: shell, resource: "uv run --no-sync lox results *", effect: allow }
---

1. Run `uv run --no-sync lox sweep <slug>`. The CLI checks review, expands the
   strategy's Python grid, runs bounded parallel batches, resumes completed
   training rows, picks finalists on training ONLY and evaluates them held out.
2. A background shell job must finish before you report. Do not start a duplicate
   sweep, poll, tune on validation, change code/config, or write unit tests.
3. Return DONE with results path and `lox results` summary. On runtime errors
   return REPAIR with the exact error; missing data/capability means BLOCKED.
   STOP means stop and tell the parent to preserve the checkpoint.
