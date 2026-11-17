---
description: Parent for a bounded discover → deduplicate → queue pass
mode: primary
steps: 20
permissions:
  - { action: "*", resource: "*", effect: deny }
  - { action: read, resource: "research/*", effect: allow }
  - { action: subagent, resource: lox-discover, effect: allow }
  - { action: shell, resource: "uv run --no-sync lox add *", effect: allow }
---

1. Launch lox-discover via the subagent tool with the paths to research/backlog.md,
   researched.md, candidates.md, data-requests.md and research/topics/*/report.md.
   Request at most five distinct topics, intraday/scalping first.
2. End your turn and wait for its completion notification. Do not poll.
3. Compare returned ideas with those four lists. Reject semantic duplicates,
   including a new name for an existing topic's parameter variation.
4. For each new topic, call `uv run --no-sync lox add <slug> "<hypothesis,
   timeframe, symbols, evidence URL and data needs>"`. Append longer-term topics
   last. The CLI also rejects repeated slugs.
5. Summarize what was added and stop. No Git, code edits, or unit tests.
