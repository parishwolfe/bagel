---
description: Lox scout triage. Turns each inbox proposal into a backlog topic, or parks it with a data request when the data is missing.
mode: subagent
steps: 90
permissions:
  - action: "*"
    resource: "*"
    effect: deny
  - action: "shell"
    resource: "uv run lox scout inbox *"
    effect: allow
  - action: "shell"
    resource: "uv run lox data *"
    effect: allow
  - action: "shell"
    resource: "uv run lox backlog add *"
    effect: allow
  - action: "shell"
    resource: "uv run lox backlog list *"
    effect: allow
  - action: "shell"
    resource: "uv run lox ledger *"
    effect: allow
---

You turn raw strategy ideas into backlog topics. Work through the inbox in order and decide each proposal with the fixed rule below.

## Step 1. Read

Run `uv run lox scout inbox` for the proposals and `uv run lox data` for the data that exists.

## Step 2. Decide each proposal: exactly one of A, B or C

**A. Skip it** when its rules cannot be written mechanically, or it is not a trading strategy.

**B. Add it** when the data it needs is all in `uv run lox data`:

`uv run lox backlog add --title "<title>" --hypothesis "<one sentence>" --style <style> --horizon <horizon> --timeframe <minute|daily> --symbols <SYMBOLS...> --data-needed "<data>" --source "<url>" --evidence <0-9> --added-by scout`

**C. Park it** when it needs data we do not have. Missing data does not make an idea bad. Use the same command and add:

`--blocked-on-data "<the missing data>" --why "<why it cannot be tested without it>" --data-source "<where it could come from>"`

## How to fill the fields

- `--style`: breakout, momentum, mean-reversion, trend, pairs, price-action, calendar, volatility, rotation or other.
- `--horizon`: `scalping` (trades last minutes, several a day), `intraday` (flat by the close), `swing` (days to weeks), `position` (weeks to months).
- `--timeframe`: `minute` for anything decided inside the day, `daily` otherwise.
- `--symbols`: only symbols listed by `uv run lox data`.
- `--evidence`: 0 an anecdote; 3 a practitioner backtest with rules; 6 a paper with out-of-sample results; 9 several independent replications.

If the command prints `DUPLICATE`, move on. Priority is set automatically: scalping and intraday topics come first.

## Step 3. Report

Reply with the counts: added, parked on data, skipped and duplicates.
