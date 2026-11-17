"""The fixed eight-step pipeline for one topic, and the gates between steps.

The step order is decided here, not by a model. After each step the strategy
lead runs ``lox gate``, ``lox sweep``, ``lox validate`` or ``lox record`` and
the command prints a ``NEXT:`` line saying exactly what to do: go on, redo a
step with a given message, or skip to recording. ``lox next`` prints the same
instruction for a topic that is being resumed.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import time
from pathlib import Path

from bagel.config import ROOT
from lox import lint, store, sweep
from lox.engine import StrategyError, load_strategy_class
from lox.settings import load_settings

# step number -> (phase, agent or None for a command the lead runs itself)
STEPS = {
    1: ("research", "lox-research"),
    2: ("design", "lox-design"),
    3: ("code", "lox-code"),
    4: ("review", "lox-review"),
    5: ("sweep", None),
    6: ("validate", None),
    7: ("analyze", "lox-analyze"),
    8: ("record", None),
}
STEP_OF = {phase: number for number, (phase, _) in STEPS.items()}
GATED = ("research", "design", "code", "review", "analyze")

BRIEF_HEADINGS = (
    "## Hypothesis",
    "## Rules",
    "## Parameters",
    "## Variations",
    "## Data requirements",
    "## Risks and costs",
    "## Sources",
)
REPORT_HEADINGS = (
    "## Summary",
    "## What the numbers say",
    "## Sanity audit",
    "## Follow-ups",
)


# --- per-topic state -----------------------------------------------------------------


def _state_path(slug: str) -> Path:
    return store.topic_dir(slug) / "state.json"


def load_state(slug: str) -> dict:
    state = store.read_json(_state_path(slug), {})
    state.setdefault("slug", slug)
    state.setdefault("passed", {})
    state.setdefault("rounds", {})
    for key in ("code_hash", "reviewed_hash", "blocked", "failed", "recorded"):
        state.setdefault(key, None)
    state.setdefault("sweep_stalls", 0)
    return state


def save_state(state: dict) -> None:
    state["updated_at"] = store.now()
    store.write_json(_state_path(state["slug"]), state)


def code_hash(slug: str) -> str | None:
    folder = store.topic_dir(slug)
    parts = [folder / "strategy.py", folder / "spec.json"]
    if not all(p.exists() for p in parts):
        return None
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.read_bytes())
    return digest.hexdigest()


def _sweep_complete(slug: str) -> bool:
    spec, problems = sweep.load_spec(slug)
    if spec is None or problems:
        return False
    marker = sweep.results_path(slug, "train").with_name("code_hash.txt")
    if not marker.exists() or marker.read_text().strip() != code_hash(slug):
        return False  # no results yet, or results from older code
    done = {r["key"] for r in store.read_jsonl(sweep.results_path(slug, "train"))}
    return all(run["key"] in done for run in sweep.expand(slug, spec))


def next_step(slug: str) -> int | None:
    """The step that is due for a topic; ``None`` once it is recorded."""
    state = load_state(slug)
    if state["recorded"]:
        return None
    if state["failed"] or state["blocked"]:
        return 8
    passed = state["passed"]
    current = code_hash(slug)
    if not passed.get("research"):
        return 1
    if not passed.get("design"):
        return 2
    if not passed.get("code") or state["code_hash"] != current:
        return 3
    if not passed.get("review") or state["reviewed_hash"] != current:
        return 4
    if not _sweep_complete(slug):
        return 5
    if not passed.get("validate"):
        return 6
    if not passed.get("analyze"):
        return 7
    return 8


# --- instructions -----------------------------------------------------------------------


def _folder(slug: str) -> str:
    try:
        return str(store.topic_dir(slug).relative_to(ROOT))
    except ValueError:
        return str(store.topic_dir(slug))


def instruction(slug: str, step: int | None, message: str = "") -> str:
    """The literal ``NEXT:`` line for a step."""
    if step is None:
        return f"NEXT: DONE. Topic {slug} is recorded. Reply with the verdict line and stop."
    phase, agent = STEPS[step]
    if agent:
        prompt = f"Topic: {slug}. Folder: {_folder(slug)}."
        if message:
            prompt += f" Fix this first: {message}"
        return (
            f"NEXT: Step {step} ({phase}). Launch subagent `{agent}` with this prompt, "
            f'then wait for its report and run `uv run lox gate {slug} {phase}`.\nPROMPT: "{prompt}"'
        )
    command = f"uv run lox {phase} {slug}"
    return f"NEXT: Step {step} ({phase}). Run `{command}` and follow its NEXT line."


def _fail_or_redo(
    state: dict, phase: str, problems: list[str], redo_step: int | None = None
) -> str:
    """Count a failed attempt; redo the step, or give up once the cap is reached."""
    slug = state["slug"]
    limit = int(load_settings().section("pipeline")[f"max_{phase}_rounds"])
    state["rounds"][phase] = state["rounds"].get(phase, 0) + 1
    listed = "\n".join(f"  - {p}" for p in problems[:25])
    if state["rounds"][phase] >= limit:
        if phase == "analyze":  # a missing report does not change the verdict
            state["passed"]["analyze"] = True
            save_state(state)
            return (
                f"RESULT: STOP (no valid report after {limit} tries)\n{listed}\n"
                + instruction(slug, 8)
            )
        state["failed"] = (
            f"{phase} step failed its gate {limit} times: {problems[0][:300]}"
        )
        save_state(state)
        return f"RESULT: STOP ({state['failed']})\n{listed}\n" + instruction(slug, 8)
    save_state(state)
    message = " | ".join(problems[:6])[:1200]
    step = redo_step or STEP_OF[phase]
    return (
        f"RESULT: REDO (attempt {state['rounds'][phase]} of {limit})\n{listed}\n"
        + instruction(slug, step, message)
    )


def _pass(state: dict, phase: str, detail: str) -> str:
    state["passed"][phase] = True
    save_state(state)
    return f"RESULT: PASS ({detail})\n" + instruction(
        state["slug"], next_step(state["slug"])
    )


# --- gates ------------------------------------------------------------------------------


def _missing_headings(text: str, headings: tuple[str, ...]) -> list[str]:
    lines = [line.strip().lower() for line in text.splitlines()]
    return [
        h for h in headings if not any(line.startswith(h.lower()) for line in lines)
    ]


def gate_research(state: dict) -> str:
    slug = state["slug"]
    if state["blocked"]:
        return "RESULT: STOP (blocked on a data request)\n" + instruction(slug, 8)
    brief = store.topic_dir(slug) / "brief.md"
    if not brief.exists():
        return _fail_or_redo(
            state, "research", [f"{_folder(slug)}/brief.md does not exist"]
        )
    text = brief.read_text()
    problems = [
        f"missing section: {h}" for h in _missing_headings(text, BRIEF_HEADINGS)
    ]
    if len(text) < 1500:
        problems.append("the brief is too short to code from (under 1,500 characters)")
    if len(set(re.findall(r"https?://[^\s)>\]]+", text))) < 2:
        problems.append("list at least two source URLs under ## Sources")
    if problems:
        return _fail_or_redo(state, "research", problems)
    return _pass(state, "research", "brief.md has every section")


def gate_design(state: dict) -> str:
    slug = state["slug"]
    spec, problems = sweep.load_spec(slug)
    if problems:
        return _fail_or_redo(state, "design", problems)
    runs = sweep.expand(slug, spec)
    return _pass(
        state, "design", f"{len(spec['variants'])} variations, {len(runs)} runs planned"
    )


def gate_code(state: dict) -> str:
    slug = state["slug"]
    spec, problems = sweep.load_spec(slug)
    if problems:
        state["passed"]["design"] = False
        return _fail_or_redo(state, "design", problems)
    path = sweep.strategy_path(slug)
    problems = lint.check_file(path)
    if problems:
        return _fail_or_redo(state, "code", ["lint: " + p for p in problems])

    runs = sweep.expand(slug, spec)
    for variant in spec["variants"]:
        try:
            load_strategy_class(path, variant["class"])
        except StrategyError as exc:
            problems.append(f"variation {variant['name']}: {exc}")
    if problems:
        return _fail_or_redo(state, "code", problems)

    # Smoke test: first, middle and last parameter set of each variation.
    for variant in spec["variants"]:
        mine = [r for r in runs if r["variant"] == variant["name"]]
        sample = {id(r): r for r in (mine[0], mine[len(mine) // 2], mine[-1])}.values()
        trades = 0
        for run in sample:
            row = sweep._execute(sweep._task(slug, spec, run, "smoke", "base"))
            if row["status"] != "ok":
                problems.append(
                    f"variation {variant['name']} with {json.dumps(run['params'])} on "
                    f"{' '.join(run['symbols'])}: {row['status']}: {row.get('error')}"
                )
                break
            trades += row["trades"]
        else:
            if trades == 0:
                problems.append(
                    f"variation {variant['name']} made no trades in the smoke window with its "
                    "first, middle or last parameter set; check the entry rule and the grid"
                )
    if problems:
        return _fail_or_redo(state, "code", problems)
    state["code_hash"] = code_hash(slug)
    return _pass(
        state, "code", "lint clean; every variation ran and traded in the smoke window"
    )


def gate_review(state: dict) -> str:
    slug = state["slug"]
    folder = store.topic_dir(slug)
    review = folder / "review.md"
    current = code_hash(slug)

    # A missing, stale or unreadable review is the reviewer's miss, not a failed
    # review of the code, so it has its own small retry budget.
    problem = None
    found = None
    if not review.exists():
        problem = f"{_folder(slug)}/review.md does not exist"
    elif review.stat().st_mtime < (folder / "strategy.py").stat().st_mtime:
        problem = "review.md is older than strategy.py; review the current code"
    else:
        found = re.search(
            r"^\s*VERDICT:\s*(PASS|FAIL)\b", review.read_text(), re.I | re.M
        )
        if not found:
            problem = (
                "review.md must start with the line `VERDICT: PASS` or `VERDICT: FAIL`"
            )
    if problem:
        misses = state["rounds"]["review_missing"] = (
            state["rounds"].get("review_missing", 0) + 1
        )
        if misses > 3:
            state["failed"] = f"no usable review after {misses - 1} tries: {problem}"
            save_state(state)
            return f"RESULT: STOP ({state['failed']})\n" + instruction(slug, 8)
        save_state(state)
        return f"RESULT: REDO ({problem})\n" + instruction(slug, 4, problem)

    if found.group(1).upper() == "FAIL":
        state["passed"]["code"] = False
        return _fail_or_redo(
            state,
            "review",
            [
                f"the review found problems; fix every finding in {_folder(slug)}/review.md"
            ],
            redo_step=3,
        )
    state["reviewed_hash"] = current
    return _pass(state, "review", "reviewer passed the code")


def gate_analyze(state: dict) -> str:
    slug = state["slug"]
    report = store.topic_dir(slug) / "report.md"
    if not report.exists():
        return _fail_or_redo(
            state, "analyze", [f"{_folder(slug)}/report.md does not exist"]
        )
    missing = _missing_headings(report.read_text(), REPORT_HEADINGS)
    if missing:
        return _fail_or_redo(
            state, "analyze", [f"missing section: {h}" for h in missing]
        )
    return _pass(state, "analyze", "report.md has every section")


GATES = {
    "research": gate_research,
    "design": gate_design,
    "code": gate_code,
    "review": gate_review,
    "analyze": gate_analyze,
}


def _out_of_order(state: dict, phase: str) -> str | None:
    due = next_step(state["slug"])
    if due is None:
        return instruction(state["slug"], None)
    if state["failed"] or state["blocked"]:
        return "RESULT: STOP\n" + instruction(state["slug"], 8)
    if due < STEP_OF[phase]:
        return (
            f"RESULT: OUT OF ORDER (step {STEP_OF[phase]} is not due yet)\n"
            + instruction(state["slug"], due)
        )
    return None


def run_gate(slug: str, phase: str) -> str:
    state = load_state(slug)
    return _out_of_order(state, phase) or GATES[phase](state)


# --- deterministic steps -----------------------------------------------------------------


def run_sweep_step(slug: str, budget_seconds: float | None = None) -> str:
    state = load_state(slug)
    early = _out_of_order(state, "sweep")
    if early:
        return early
    spec, _ = sweep.load_spec(slug)
    # Results are keyed by parameters, so they are only valid for the code that
    # produced them: start over if the strategy or the spec changed.
    marker = sweep.results_path(slug, "train").with_name("code_hash.txt")
    if marker.exists() and marker.read_text().strip() != state["code_hash"]:
        shutil.rmtree(marker.parent)
        state["passed"]["validate"] = state["passed"]["analyze"] = False
    store.write_text_atomic(marker, f"{state['code_hash']}\n")
    progress = sweep.run_sweep(slug, spec, budget_seconds)
    if progress["stalled"]:
        state["sweep_stalls"] += 1
        if state["sweep_stalls"] >= 2:
            state["failed"] = "the sweep hung twice; a backtest never returned"
            save_state(state)
            return f"RESULT: STOP ({state['failed']})\n" + instruction(slug, 8)
    save_state(state)
    if progress["done"] < progress["total"]:
        return (
            f"RESULT: PROGRESS {progress['done']}/{progress['total']} runs\n"
            f"NEXT: Step 5 (sweep). Run `uv run lox sweep {slug}` again."
        )
    summary = sweep.write_sweep_summary(slug, spec)
    clean = summary["by_status"].get("ok", 0)
    if clean == 0:
        sample = (
            summary["sample_errors"][0]["error"] if summary["sample_errors"] else ""
        )
        state["failed"] = (
            f"all {summary['runs']} sweep runs failed: {str(sample)[:300]}"
        )
        save_state(state)
        return f"RESULT: STOP ({state['failed']})\n" + instruction(slug, 8)
    return (
        f"RESULT: COMPLETE {progress['total']}/{progress['total']} runs ({clean} clean)\n"
        + instruction(slug, 6)
    )


def run_validate_step(slug: str) -> str:
    state = load_state(slug)
    early = _out_of_order(state, "validate")
    if early:
        return early
    spec, _ = sweep.load_spec(slug)
    result = sweep.validate(slug, spec)
    state["passed"]["validate"] = True
    save_state(state)
    return (
        f"RESULT: VERDICT {result['verdict']} ({result['reason']})\n"
        f"Details: {_folder(slug)}/validation.md\n" + instruction(slug, 7)
    )


# --- recording ----------------------------------------------------------------------------


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def git_sha() -> str | None:
    done = _git("rev-parse", "HEAD")
    return done.stdout.strip() if done.returncode == 0 else None


def _commit(slug: str, verdict: str) -> str:
    """Commit research/ on the configured branch only; never force, never main."""
    cfg = load_settings().section("git")
    research = store.research_dir().resolve()
    if not cfg["enabled"]:
        return "git: disabled in settings"
    if ROOT.resolve() not in research.parents:
        return "git: skipped (research directory is outside the repository)"
    branch = _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if branch != cfg["branch"]:
        return f"git: skipped (on branch '{branch}', commits only happen on '{cfg['branch']}')"
    relative = str(research.relative_to(ROOT.resolve()))
    _git("add", "--", relative)
    if _git("diff", "--cached", "--quiet", "--", relative).returncode == 0:
        return "git: nothing to commit"
    message = f"lox: {slug} -> {verdict}"
    done = _git("commit", "-m", message, "--", relative)
    if done.returncode != 0:  # a formatting hook may have rewritten files: restage once
        _git("add", "--", relative)
        done = _git("commit", "-m", message, "--", relative)
    if done.returncode != 0:
        return f"git: commit failed: {(done.stderr or done.stdout).strip()[:300]}"
    if not cfg["push"]:
        return "git: committed (push is off in settings)"
    pushed = _git("push", cfg["remote"], f"HEAD:refs/heads/{cfg['branch']}")
    if pushed.returncode != 0:
        return f"git: committed; push failed: {pushed.stderr.strip()[:300]}"
    return f"git: committed and pushed to {cfg['remote']}/{cfg['branch']}"


def record(slug: str, fail_reason: str | None = None) -> str:
    state = load_state(slug)
    if state["recorded"]:
        return f"RESULT: ALREADY RECORDED verdict={state['recorded']}\n" + instruction(
            slug, None
        )
    if fail_reason and not state["failed"]:
        state["failed"] = fail_reason
    card = store.get_topic(slug) or {"slug": slug, "title": slug, "aliases": []}
    folder = store.topic_dir(slug)
    verdict_file = store.read_json(folder / "verdict.json", None)
    due = next_step(slug)

    best = None
    if state["blocked"]:
        verdict, reason = "needs_data", state["blocked"]
    elif state["failed"]:
        verdict, reason = "failed", state["failed"]
    elif verdict_file is None or (due is not None and due < 7):
        return f"RESULT: NOT READY (step {due} is due)\n" + instruction(slug, due)
    else:
        verdict, reason, best = (
            verdict_file["verdict"],
            verdict_file["reason"],
            verdict_file["best"],
        )

    settings = load_settings()
    spec = (
        store.read_json(folder / "spec.json", {})
        if (folder / "spec.json").exists()
        else {}
    )
    timeframe = spec.get("timeframe") or card.get("timeframe")
    row = {
        "slug": slug,
        "title": card.get("title", slug),
        "aliases": card.get("aliases", []),
        "style": card.get("style"),
        "horizon": card.get("horizon"),
        "timeframe": timeframe,
        "verdict": verdict,
        "reason": reason,
        "trials": verdict_file["trials"] if verdict_file else 0,
        "recorded_at": store.now(),
        "git_sha": git_sha(),
        "code_sha256": code_hash(slug),
        "capital": settings.capital,
        "costs": settings.raw["costs"],
        "windows": settings.raw["windows"].get(timeframe) if timeframe else None,
    }
    candidate = None
    if best:
        v = best["validate"]
        row.update(
            {
                "best_variant": best["variant"],
                "best_symbols": best["symbols"],
                "best_params": best["params"],
                "train_annual_return": best["train"]["annual_return"],
                "validate_annual_return": v.get("annual_return"),
                "benchmark_annual_return": v.get("benchmark_annual_return"),
                "validate_max_drawdown": v.get("max_drawdown"),
                "validate_trades": v.get("trades"),
            }
        )
        if verdict == "candidate":
            candidate = {
                "slug": slug,
                "title": row["title"],
                "variant": best["variant"],
                "class": best["class"],
                "symbols": best["symbols"],
                "params": best["params"],
                "train": best["train"],
                "validate": best["validate"],
                "stress": best["stress"],
                "neighbours": best["neighbours"],
                "trials": row["trials"],
                "code_sha256": row["code_sha256"],
                "recorded_at": row["recorded_at"],
                "status": "needs follow-up testing and paper trading",
            }
    store.record_result(row, candidate)

    control = store.load_control()
    control["consecutive_failures"] = (
        control["consecutive_failures"] + 1 if verdict == "failed" else 0
    )
    store.save_control(control)

    state["recorded"] = verdict
    save_state(state)
    git_note = _commit(slug, verdict)
    return f"RESULT: RECORDED verdict={verdict} ({reason})\n{git_note}\n" + instruction(
        slug, None
    )


def block_on_data(slug: str, reason: str) -> None:
    state = load_state(slug)
    state["blocked"] = reason
    save_state(state)


# --- orchestrator helpers --------------------------------------------------------------------


def status(slug: str) -> str:
    state = load_state(slug)
    if state["recorded"]:
        return f"DONE {slug} verdict={state['recorded']}"
    due = next_step(slug)
    path = _state_path(slug)
    idle = (time.time() - path.stat().st_mtime) / 60 if path.exists() else 0.0
    stalled = idle > load_settings().section("pipeline")["stall_minutes"]
    label = "STALLED" if stalled else "IN PROGRESS"
    return f"{label} {slug} step={due} ({STEPS[due][0]}) idle_minutes={idle:.0f}"


def stop_reason() -> str | None:
    """Why the orchestrator must not start another topic, if there is a reason."""
    cfg = load_settings().section("pipeline")
    root = store.research_dir()
    if (root / "STOP").exists():
        return f"the stop file {root / 'STOP'} exists"
    failures = store.load_control()["consecutive_failures"]
    if failures >= cfg["max_consecutive_failures"]:
        return (
            f"{failures} topics in a row ended as 'failed'; something is broken. "
            "A human should look, then run `lox resume`"
        )
    free_gb = shutil.disk_usage(root if root.exists() else ROOT).free / 1e9
    if free_gb < cfg["min_free_disk_gb"]:
        return f"only {free_gb:.1f} GB of disk is free (floor is {cfg['min_free_disk_gb']} GB)"
    return None
