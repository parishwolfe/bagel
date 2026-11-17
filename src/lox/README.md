# Lox

Lox researches trading strategies on top of bagel. OpenCode agents read about a
strategy, code several variations of it, backtest every parameter combination,
and judge the best ones on data they were not tuned on. Results land in four
files you read.

Nothing here places an order. The output is a list of candidates for you to
follow up by hand.

## Install

```bash
src/lox/install.sh              # this checkout
src/lox/install.sh --dry-run    # show what would change
src/lox/install.sh --harden     # also write hard-deny policies (dedicated machine only)
src/lox/install.sh --uninstall
```

The script needs `uv` and `git` (and `opencode` 2.3 or newer to run the agents).
It runs `uv sync`, creates `research/` with the seed backlog, copies the agents
and commands into `.opencode/`, sets `experimental.subagent_depth` to 2 in
`.opencode/opencode.json`, and finishes with `uv run lox doctor`. It never
touches providers, models or MCP servers, and it needs no sudo.

The data catalog is not in git. Copy `data/catalog` from a machine that has it,
or set `CATALOG_PATH`, or run `uv run bagel-download --timeframe all` against an
IB Gateway.

Models: agents inherit the session's model unless you pin one, for example
`--model review=anthropic/claude-sonnet-5-5 --model all=ollama/qwen3.8:27b`.
A different model for `review` than for `code` is the pairing that pays off most.

## Run

Start OpenCode (or OpenChamber) in the checkout:

| Command | What it does |
| --- | --- |
| `/lox-run` | Work through the backlog, one topic at a time, until it is empty. |
| `/lox-scout` | One scouting pass that adds topics to the backlog. |
| `/lox-one <slug>` | Run the pipeline for one topic in the current session. |
| `/lox-status` | Counts and the topic in progress. |

To stop cleanly, run `uv run lox stop`: the orchestrator ends after the current
topic. `uv run lox resume` clears it. An interrupted run resumes where it left
off the next time you start `/lox-run`.

## How it works

Three layers:

- **`lox` CLI** (this package): state files, the backtest harness, and the gates
  between steps. All of it is deterministic.
- **Agents** (`opencode/agents`): each does one job and follows numbered steps.
- **`research/`**: the only place agents can write.

The orchestrator (`lox`) claims a topic and launches one strategy lead
(`lox-strategy`). The lead runs eight fixed steps and launches one subagent per
step that needs a model:

| Step | Who | Output | Gate |
| --- | --- | --- | --- |
| 1 Research | `lox-research` | `brief.md` | has every section and sources, or a data request was filed |
| 2 Design | `lox-design` | `spec.json` | at least 3 variations, valid grids, symbols exist |
| 3 Code | `lox-code` | `strategy.py` | lint clean; every variation runs and trades in a smoke test |
| 4 Review | `lox-review` | `review.md` | `VERDICT: PASS`; a fail sends it back to step 3 |
| 5 Sweep | `lox sweep` | `sweep_summary.md` | every run finished |
| 6 Validate | `lox validate` | `validation.md`, `verdict.json` | the flag rule |
| 7 Analyze | `lox-analyze` | `report.md`, follow-up topics | has every section |
| 8 Record | `lox record` | ledger row, candidate row, git commit | |

Neither the orchestrator nor the lead decides what happens next. Every `lox`
command prints a `NEXT:` line with the exact step, agent and prompt, and the
gates refuse steps run out of order. A step that keeps failing its gate is
retried a fixed number of times, then the topic is recorded as `failed`.

Between topics the orchestrator runs `uv run lox compact`, which asks OpenCode
to compact its own session.

The scout (`lox-scout`) launches one `lox-scout-harvest` per source lane
(academic, practitioner, price action, past results, daily-bar ideas), then
`lox-scout-triage`, which adds each idea to the backlog or parks it on missing
data. Duplicates are refused by the CLI. Scalping and intraday topics get the
highest priority automatically.

## The four files

All under `research/`, each with a JSON store and a Markdown view:

| View | Holds |
| --- | --- |
| `BACKLOG.md` | Topics still to research, highest priority first. |
| `RESEARCHED.md` | Every finished topic with its verdict, so nothing is researched twice. |
| `CANDIDATES.md` | Topics that passed the flag rule. Follow these up. |
| `DATA_REQUESTS.md` | Topics that need data the catalog lacks. |

Verdicts: `candidate` (passed everything), `watch` (profitable and ahead of
buy-and-hold on held-out data, but short of the rule), `rejected`, `needs_data`
(parked, not judged), `failed` (the pipeline could not finish it).

A strategy that needs data you do not have is never called non-viable. The
researcher files a data request: blocking parks the topic as `needs_data`;
`--partial` records the request and tests the reduced form that current data
supports. When the data arrives, `uv run lox backlog requeue <slug>`.

Each topic's working files are in `research/topics/<slug>/`.

## The flag rule

Parameters are chosen on the train window and judged on the validate window.
Everything after the validate window is a vault the harness cannot read; keep it
for your own follow-up.

A configuration is a candidate when, on the validate window, it:

- returns at least 36% a year after costs,
- beats buying and holding the same instrument,
- makes at least 30 trades,
- is still profitable under stress costs, and
- has neighbours in the parameter grid of which at least half were profitable on train.

Backtests start with $50,000, trade long and short, and are thrown out if gross
exposure passes 1x equity. Base costs are $0 commission (IBKR Lite) plus one
tick of slippage on every fill; stress costs add one cent a share. Windows,
thresholds, costs and limits are all in [`settings.toml`](settings.toml), which
agents cannot edit.

## Guardrails

- **Fixed procedure.** Step order and retry limits live in the CLI.
- **Permissions.** Every agent starts from deny-everything. Each may run only
  the `uv run lox ...` subcommands it needs and write only its own file under
  `research/topics/*/`. Only the research and harvest agents have web access,
  and fetching `ssrn.com` or local addresses is denied.
- **Generated code.** `strategy.py` is linted before it is ever imported: a
  short import allowlist, no files, network, `eval`, or Nautilus live adapters.
  Each backtest has a time limit, sweep workers run at low priority and leave
  two cores free, and an exception inside a strategy is reported as an error
  (Nautilus alone would log it and carry on with a quiet, wrong result).
- **Run control.** A stop file, a halt after three `failed` topics in a row, a
  free-disk floor, and a cap on runs per topic.
- **Git.** Agents have no git access. `lox record` commits `research/` only when
  the checkout is on `lox/research`, and pushes only if `git.push` is on.
- **No trading.** No agent can reach IB, and strategy code cannot import the
  adapters.

The linter stops a confused model, not a determined attacker. The real boundary
is the operating-system user.

### Dedicated user

For unattended runs, create a normal (non-admin) user, clone the repository
into its home, and install there with `--harden`. Give that user read-only
access to the data catalog and no IB credentials. `uv run lox doctor` warns
when lox is running as a user who can administer the machine.

### Pushing results

1. On the research machine: `git switch -c lox/research`.
2. Protect `main` on the remote so the token cannot write to it. A fine-grained
   token cannot be limited to one branch, so branch protection does that part.
3. Create a fine-grained token with contents read and write on this repository
   only, and store it in that user's git credential helper. Keep it out of the
   repository and out of `.env`.
4. Set `push = true` under `[git]` in `settings.toml`.

Lox pushes with `git push origin HEAD:refs/heads/lox/research`, never with force.

## MCP servers

Not configured yet. Agents deny every tool they are not granted, so after
adding a server to `mcp.servers`, also allow its tools for `lox-research` and
`lox-scout-harvest` in `opencode/agents/` and reinstall:

```yaml
  - action: "<server>_*"
    resource: "*"
    effect: allow
```

## Layout

```
src/lox/
  cli.py            the `lox` command
  settings.toml     account, costs, windows, flag rule, limits
  store.py          the four state files and their Markdown views
  pipeline.py       the eight steps, gates, recording, git
  sweep.py          spec validation, parallel sweep, validation, flag rule
  engine.py         one backtest: sizing, costs, equity curve, benchmark
  api.py            LoxStrategy, the base class generated strategies use
  lint.py           static checks on generated code
  scout.py          scouting lanes and inbox
  installer.py      `lox install` and `lox doctor`
  opencode/         agents and commands (the source of truth)
  docs/             strategy_guide.md, read by the code and review agents
  examples/orb/     a reference strategy and spec
  seeds/            the starting backlog
research/           state and per-topic folders (created by `lox init`)
```

## Limits to know about

- **Two years of 1-minute bars** means roughly 14 months of train and 8 of
  validate. A candidate is a hypothesis for more testing, not a finding.
- **The validate window is reused** by every topic. The more topics pass through
  it, the more likely one clears it by luck; the ledger records how many
  configurations each topic tried.
- **Fills** happen at the close of the bar that produced the signal, one tick
  against you. Stop orders are simulated against bar highs and lows.
- **A run holds minute bars or daily bars, not both.**
- **Equities only.** ES futures are in the catalog but one contract is several
  times the account.
- **If a lead dies mid-topic** the orchestrator waits. Start `/lox-run` again
  and it resumes the same topic at the step that was due.
