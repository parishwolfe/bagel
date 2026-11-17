---
description: Phase 3 — independent logic review before the parameter sweep
mode: subagent
steps: 16
permissions:
  - { action: "*", resource: "*", effect: deny }
  - { action: read, resource: "research/*", effect: allow }
  - { action: read, resource: "src/lox/*", effect: allow }
  - { action: read, resource: "src/bagel/strategies/*", effect: allow }
  - { action: edit, resource: "research/topics/*/review.md", effect: allow }
---

1. Read the brief, strategy, checked.sha256 and smoke.csv yourself.
2. Trace entries, exits, sizing and every parameter to their behavior. Check
   look-ahead, warmup, DST/session boundaries, missing bars, pending orders,
   rejected orders, open positions, and whether each claimed variation differs.
   Compare assumptions to `src/lox/worker.py` (next-event execution, cash account,
   one tick slippage, daily-close measurement).
3. Write `research/topics/<slug>/review.md`, starting exactly `PASS` or `FAIL`,
   then `Fingerprint: <contents of checked.sha256>`. Include concrete evidence
   and line references. FAIL includes actionable fixes; never edit the strategy.
4. Return DONE for PASS, or REPAIR for FAIL. A successful smoke run alone is not
   evidence that the financial logic is correct. Do not write unit tests.
