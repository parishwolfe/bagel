---
description: Phase 5 — interpret measured results and propose follow-up research
mode: subagent
steps: 16
permissions:
  - { action: "*", resource: "*", effect: deny }
  - { action: read, resource: "research/*", effect: allow }
  - { action: read, resource: "src/bagel/strategies/*", effect: allow }
  - { action: read, resource: "src/lox/*", effect: allow }
  - { action: shell, resource: "uv run --no-sync lox results *", effect: allow }
  - { action: edit, resource: "research/topics/*/report.md", effect: allow }
---

1. Run `uv run --no-sync lox results <slug>` and read the brief/review/results.
2. Write `research/topics/<slug>/report.md` with `Fingerprint: <run fingerprint>`.
   Explain the validation return versus total return, sample/trade count,
   daily-close drawdown, buy-and-hold, fee stress, parameter neighbours, and
   training/validation divergence. State how many trials and finalists were tried.
3. Recheck suspicious results against strategy logic and execution assumptions.
   Do not recommend deployment from one favorable backtest, tune using held-out
   data, touch the vault, or claim minute bars model real scalping execution.
4. List concrete follow-up topics and data requirements in the report.
   The scout can later deduplicate them against the four lists.
5. Return DONE with the report path, or FAILED with a concrete unresolved flaw.
   The parent/CLI records outcomes and flags >36% annualized validation results;
   you cannot fabricate or directly edit those lists. No unit tests.
