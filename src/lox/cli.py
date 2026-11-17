"""``lox``: the one command the agents are allowed to run.

Every state change and every backtest goes through here. Commands that agents
call print a ``RESULT:`` line and, where there is a next action, a ``NEXT:``
line that says exactly what to do, so no model has to work out the order.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from lox import pipeline, scout, store, sweep
from lox.settings import load_settings

SEEDS = Path(__file__).with_name("seeds") / "backlog.json"


def _human_only(command: str) -> None:
    """Refuse commands that change the setup when called from an agent's shell."""
    if os.environ.get("OPENCODE_SESSION_ID"):
        sys.exit(
            f"REFUSED: `lox {command}` is for the human operator, not for agents. "
            "Do not retry it; carry on with your steps."
        )


# --- orchestrator ---------------------------------------------------------------


def cmd_claim(args) -> None:
    reason = pipeline.stop_reason()
    if reason:
        print(
            f"STOP: {reason}.\nNEXT: Report this to the user and end. Do not claim again."
        )
        return
    session = os.environ.get("OPENCODE_SESSION_ID")
    current = next(
        (c for c in store.load_backlog() if c["status"] == "in_progress"), None
    )
    if current and current.get("claimed_by") not in (None, session) and not args.force:
        state_file = store.topic_dir(current["slug"]) / "state.json"
        idle = time.time() - state_file.stat().st_mtime if state_file.exists() else 1e9
        if idle < 600:
            print(
                f"STOP: another session is working on {current['slug']} "
                f"(active {idle / 60:.0f} minutes ago).\n"
                "NEXT: Report this to the user and end."
            )
            return
    card = store.claim_next(session)
    if card is None:
        print(
            "EMPTY: no topic on the backlog is ready.\n"
            "NEXT: Reply with a summary of this session's verdicts and end. "
            "The user can run the scout to add topics."
        )
        return
    slug = card["slug"]
    print(
        f"CLAIMED: {slug}\nTITLE: {card['title']}\n"
        f"NEXT: Launch subagent `lox-strategy` with this prompt, then end your turn and wait.\n"
        f'PROMPT: "Run the lox pipeline for topic: {slug}"'
    )


def cmd_status(args) -> None:
    if args.slug:
        print(pipeline.status(args.slug))
        return
    backlog = store.load_backlog()
    ledger = store.load_ledger()
    verdicts: dict[str, int] = {}
    for row in ledger:
        verdicts[row["verdict"]] = verdicts.get(row["verdict"], 0) + 1
    print(f"backlog: {sum(c['status'] == 'ready' for c in backlog)} ready")
    for card in backlog:
        if card["status"] == "in_progress":
            print("in progress: " + pipeline.status(card["slug"]))
    print(
        "researched: "
        + (", ".join(f"{n} {v}" for v, n in sorted(verdicts.items())) or "none")
    )
    print(f"candidates: {len(store.load_candidates())}")
    print(
        f"open data requests: {sum(r['status'] == 'open' for r in store.load_data_requests())}"
    )
    print(f"stop: {pipeline.stop_reason() or 'no'}")


def cmd_compact(args) -> None:
    session = os.environ.get("OPENCODE_SESSION_ID")
    if not session:
        print("SKIPPED: not running inside an OpenCode session.")
        return
    try:
        done = subprocess.run(
            ["opencode", "api", "post", f"/api/session/{session}/compact", "--data", "{}"],
            capture_output=True, text=True, timeout=60, check=False,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"SKIPPED: could not request compaction ({type(exc).__name__}).")
        return
    if done.returncode == 0:
        print("REQUESTED: this session will be compacted at the next safe point.")
    else:
        print(f"SKIPPED: compaction request failed: {done.stderr.strip()[:200]}")


# --- strategy lead ------------------------------------------------------------------


def cmd_next(args) -> None:
    print(pipeline.instruction(args.slug, pipeline.next_step(args.slug)))


def cmd_gate(args) -> None:
    print(pipeline.run_gate(args.slug, args.phase))


def cmd_sweep(args) -> None:
    print(pipeline.run_sweep_step(args.slug, args.budget_seconds))


def cmd_validate(args) -> None:
    print(pipeline.run_validate_step(args.slug))


def cmd_record(args) -> None:
    print(pipeline.record(args.slug, args.fail))


# --- phase agents ----------------------------------------------------------------------


def cmd_data(args) -> None:
    from lox.engine import inventory

    settings = load_settings()
    rows = inventory()
    print("Data in the catalog (this is everything lox can backtest on):\n")
    print(
        store._table(
            ["Symbol", "Bars", "From", "To", "Usable now"],
            [
                [
                    r["symbol"],
                    r["timeframe"],
                    r["start"],
                    r["end"],
                    "yes"
                    if r["tradable"]
                    else "no (futures: one contract exceeds the account)",
                ]
                for r in rows
            ],
        )  # fmt: skip
    )
    for timeframe in ("minute", "daily"):
        w = settings.windows(timeframe)
        print(
            f"{timeframe} windows: train {w.train_start} to {w.train_end}, "
            f"validate {w.validate_start} to {w.validate_end} (later data is reserved)."
        )
    print(
        f"\nAccount: ${settings.capital:,.0f}, long and short, gross exposure up to "
        f"{settings.max_leverage:g}x equity.\n"
        "Bars are OHLCV, regular trading hours only (09:30 to 16:00 New York).\n"
        "NOT available: pre-market and after-hours bars, quotes, ticks, order book depth, "
        "options, fundamentals, news, sentiment, short interest, other symbols, futures and "
        "crypto. If a strategy needs any of these, file it with `lox data-request` instead of "
        "calling it non-viable."
    )


def cmd_data_request(args) -> None:
    if store.get_topic(args.slug) is None:
        sys.exit(f"ERROR: unknown topic {args.slug}")
    store.add_data_request(
        slug=args.slug,
        data=args.data,
        why=args.why,
        source=args.source,
        blocking=not args.partial,
    )
    if args.partial:
        print(
            "RESULT: RECORDED a non-blocking data request.\n"
            "NEXT: Carry on with the reduced form of the strategy that the current data supports."
        )
    else:
        pipeline.block_on_data(args.slug, f"needs data: {args.data}")
        print(
            "RESULT: RECORDED. The topic is parked as needs_data, not rejected.\n"
            "NEXT: Stop work on this topic and report that it is blocked on data."
        )


def cmd_lint(args) -> None:
    from lox import lint

    problems = lint.check_file(sweep.strategy_path(args.slug))
    print(
        "RESULT: CLEAN"
        if not problems
        else "RESULT: PROBLEMS\n" + "\n".join(f"  - {p}" for p in problems)
    )


def cmd_backtest(args) -> None:
    spec, problems = sweep.load_spec(args.slug)
    if problems:
        sys.exit(
            "ERROR: spec.json is not valid:\n" + "\n".join(f"  - {p}" for p in problems)
        )
    variant = next((v for v in spec["variants"] if v["name"] == args.variant), None)
    if variant is None:
        names = ", ".join(v["name"] for v in spec["variants"])
        sys.exit(f"ERROR: no variation named {args.variant}; spec.json has: {names}")
    try:
        params = (
            json.loads(args.params)
            if args.params
            else {k: v[0] for k, v in variant["grid"].items()}
        )
    except json.JSONDecodeError as exc:
        sys.exit(f"ERROR: --params is not valid JSON: {exc}")
    symbols = args.symbols or (
        spec["symbols"] if spec.get("mode") == "basket" else spec["symbols"][:1]
    )
    run = {
        "key": "adhoc", "variant": variant["name"], "class": variant["class"],
        "symbols": [s.upper() for s in symbols], "params": params,
    }  # fmt: skip
    row = sweep._execute(sweep._task(args.slug, spec, run, args.window, "base"))
    print(json.dumps(row, indent=2))


# --- backlog, ledger, scout -----------------------------------------------------------------


def cmd_backlog_list(args) -> None:
    for card in store.load_backlog():
        print(
            f"{card['priority']:>4}  {card['status']:<11}  {card['slug']:<40}  {card['title']}"
        )


def cmd_backlog_show(args) -> None:
    card = store.get_topic(args.slug)
    if card is None:
        sys.exit(f"ERROR: unknown topic {args.slug}")
    print(json.dumps(card, indent=2))


def cmd_backlog_add(args) -> None:
    try:
        card = store.make_card(
            title=args.title, hypothesis=args.hypothesis, style=args.style, horizon=args.horizon,
            timeframe=args.timeframe, symbols=args.symbols, data_needed=args.data_needed or "",
            sources=args.source, aliases=args.alias, evidence=args.evidence, parent=args.parent,
            added_by=args.added_by,
        )  # fmt: skip
    except ValueError as exc:
        sys.exit(f"ERROR: {exc}")

    if args.blocked_on_data:
        if not args.why:
            sys.exit("ERROR: --blocked-on-data needs --why")
        duplicate = store.find_duplicate(card, store.known_topics())
        if duplicate:
            print(f"RESULT: DUPLICATE of {duplicate}. Nothing added.")
            return
        store.write_json(store.topic_dir(card["slug"]) / "topic.json", card)
        store.add_data_request(
            slug=card["slug"], data=args.blocked_on_data, why=args.why,
            source=args.data_source or "", blocking=True,
        )  # fmt: skip
        pipeline.block_on_data(card["slug"], f"needs data: {args.blocked_on_data}")
        pipeline.record(card["slug"])
        print(
            f"RESULT: PARKED {card['slug']} as needs_data with a data request (not rejected)."
        )
        return

    status, slug = store.add_topic(card)
    if status == "duplicate":
        print(f"RESULT: DUPLICATE of {slug}. Nothing added.")
    else:
        print(f"RESULT: ADDED {slug} with priority {card['priority']}.")


def cmd_backlog_remove(args) -> None:
    _human_only("backlog remove")
    with store.locked():
        card = store.remove_from_backlog(args.slug)
        store.render_views()
    print(f"removed {args.slug}" if card else f"{args.slug} is not on the backlog")


def cmd_backlog_requeue(args) -> None:
    _human_only("backlog requeue")
    try:
        store.requeue(args.slug)
    except ValueError as exc:
        sys.exit(f"ERROR: {exc}")
    state = pipeline.load_state(args.slug)
    state.update({"recorded": None, "blocked": None, "failed": None, "rounds": {}})
    pipeline.save_state(state)
    print(f"requeued {args.slug}")


def cmd_ledger(args) -> None:
    for row in store.load_ledger():
        if args.verdict and row["verdict"] != args.verdict:
            continue
        line = (
            f"{row['recorded_at'][:10]}  {row['verdict']:<10}  {row['slug']:<40}  "
            f"validate/yr={store._pct(row.get('validate_annual_return')) or 'n/a':<8}  {row['reason']}"
        )
        if not args.grep or args.grep.lower() in line.lower():
            print(line)


def cmd_coverage(args) -> None:
    print("Topics queued or researched, by style and horizon:\n")
    print(scout.coverage())


def cmd_scout_begin(args) -> None:
    print("Scouting pass. Lanes, in order:\n")
    for number, (lane, brief) in enumerate(scout.LANES.items(), start=1):
        print(f"{number}. {lane}: {brief}\n")
    print("Coverage so far (thin cells are where new topics help most):\n")
    print(scout.coverage())
    print(f"Proposals already in the inbox: {len(scout.inbox())}")


def cmd_scout_propose(args) -> None:
    try:
        count = scout.propose(
            lane=args.lane, title=args.title, hypothesis=args.hypothesis,
            rules=args.rules, data=args.data, sources=args.source,
        )  # fmt: skip
    except ValueError as exc:
        sys.exit(f"ERROR: {exc}")
    print(f"RESULT: PROPOSED. The inbox now holds {count}.")


def cmd_scout_inbox(args) -> None:
    rows = scout.inbox()
    if not rows:
        print("The inbox is empty.")
    for number, row in enumerate(rows, start=1):
        print(f"--- proposal {number} of {len(rows)} ---\n{json.dumps(row, indent=2)}")


def cmd_scout_end(args) -> None:
    count = scout.end()
    ready = sum(c["status"] == "ready" for c in store.load_backlog())
    print(f"RESULT: ARCHIVED {count} proposals. The backlog has {ready} ready topics.")


# --- operator ---------------------------------------------------------------------------------


def cmd_init(args) -> None:
    _human_only("init")
    added = store.init(SEEDS)
    print(f"research directory: {store.research_dir()}")
    print(
        f"seeded {len(added)} topics"
        if added
        else "backlog already present; nothing seeded"
    )


def cmd_resume(args) -> None:
    _human_only("resume")
    store.save_control({"consecutive_failures": 0})
    stop = store.research_dir() / "STOP"
    if stop.exists():
        stop.unlink()
    print("cleared the failure counter and the stop file")


def cmd_stop(args) -> None:
    (store.research_dir() / "STOP").write_text(f"stop requested {store.now()}\n")
    print("stop file written: the orchestrator ends after the current topic")


def cmd_render(args) -> None:
    with store.locked():
        store.render_views()
    print("views rendered")


def cmd_install(args) -> None:
    _human_only("install")
    from lox import installer

    installer.install(Path(args.target) if args.target else None, args)


def cmd_doctor(args) -> None:
    from lox import installer

    sys.exit(installer.doctor())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lox", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, func, help_text: str, parent=sub):
        p = parent.add_parser(name, help=help_text)
        p.set_defaults(func=func)
        return p

    p = add("claim", cmd_claim, "orchestrator: take the next backlog topic")
    p.add_argument("--force", action="store_true")
    add("status", cmd_status, "overall status, or one topic's").add_argument(
        "slug", nargs="?"
    )
    add("compact", cmd_compact, "orchestrator: ask OpenCode to compact this session")

    add("next", cmd_next, "lead: which step is due for a topic").add_argument("slug")
    p = add("gate", cmd_gate, "lead: check a step's output")
    p.add_argument("slug")
    p.add_argument("phase", choices=pipeline.GATED)
    p = add("sweep", cmd_sweep, "lead: run pending train backtests (resumable)")
    p.add_argument("slug")
    p.add_argument("--budget-seconds", type=float, default=None)
    add(
        "validate", cmd_validate, "lead: held-out validation and the flag rule"
    ).add_argument("slug")
    p = add("record", cmd_record, "lead: write the verdict to the ledger")
    p.add_argument("slug")
    p.add_argument("--fail", default=None, help="record as failed with this reason")

    add("data", cmd_data, "what data exists and what does not")
    p = add("data-request", cmd_data_request, "a topic needs data the catalog lacks")
    p.add_argument("slug")
    p.add_argument("--data", required=True, help="what data is needed")
    p.add_argument(
        "--why", required=True, help="why the strategy cannot be tested without it"
    )
    p.add_argument("--source", default="", help="where it could come from")
    p.add_argument(
        "--partial", action="store_true", help="a reduced form can still be tested now"
    )
    add("lint", cmd_lint, "check a topic's strategy.py").add_argument("slug")
    p = add("backtest", cmd_backtest, "one ad-hoc run of a variation (train data only)")
    p.add_argument("slug")
    p.add_argument("--variant", required=True)
    p.add_argument(
        "--params", default=None, help="JSON object; default: first value of each grid"
    )
    p.add_argument("--symbols", nargs="*", default=None)
    p.add_argument("--window", choices=("smoke", "train"), default="smoke")

    backlog = sub.add_parser(
        "backlog", help="the to-be-researched list"
    ).add_subparsers(dest="backlog_command", required=True)
    add("list", cmd_backlog_list, "list topics", backlog)
    add("show", cmd_backlog_show, "one topic card", backlog).add_argument("slug")
    p = add("add", cmd_backlog_add, "add a topic (de-duplicated)", backlog)
    p.add_argument("--title", required=True)
    p.add_argument("--hypothesis", required=True)
    p.add_argument("--style", required=True, choices=store.STYLES)
    p.add_argument("--horizon", required=True, choices=list(store.HORIZON_PRIORITY))
    p.add_argument("--timeframe", required=True, choices=store.TIMEFRAMES)
    p.add_argument("--symbols", nargs="+", required=True)
    p.add_argument("--data-needed", default="")
    p.add_argument("--source", action="append", default=[])
    p.add_argument("--alias", action="append", default=[])
    p.add_argument(
        "--evidence",
        type=int,
        default=0,
        help="0-9: strength of the published evidence",
    )
    p.add_argument("--parent", default=None, help="slug of the topic this follows up")
    p.add_argument("--added-by", default="agent")
    p.add_argument(
        "--blocked-on-data",
        default=None,
        help="park it: the data it needs that we lack",
    )
    p.add_argument("--why", default=None)
    p.add_argument("--data-source", default=None)
    add("remove", cmd_backlog_remove, "operator: drop a topic", backlog).add_argument(
        "slug"
    )
    add(
        "requeue", cmd_backlog_requeue, "operator: research a topic again", backlog
    ).add_argument("slug")

    p = add("ledger", cmd_ledger, "the already-researched list")
    p.add_argument("--verdict", choices=store.VERDICTS, default=None)
    p.add_argument("--grep", default=None)
    add("coverage", cmd_coverage, "topic counts by style and horizon")

    scouting = sub.add_parser("scout", help="the scouting pass").add_subparsers(
        dest="scout_command", required=True
    )
    add("begin", cmd_scout_begin, "lanes and coverage", scouting)
    p = add("propose", cmd_scout_propose, "drop a raw idea in the inbox", scouting)
    p.add_argument("--lane", required=True, choices=list(scout.LANES))
    p.add_argument("--title", required=True)
    p.add_argument("--hypothesis", required=True)
    p.add_argument("--rules", required=True, help="entry and exit rules as found")
    p.add_argument("--data", required=True, help="data the idea needs")
    p.add_argument("--source", action="append", default=[], required=True)
    add("inbox", cmd_scout_inbox, "show proposals awaiting triage", scouting)
    add("end", cmd_scout_end, "archive the inbox", scouting)

    add("init", cmd_init, "operator: create research/ and seed the backlog")
    add("resume", cmd_resume, "operator: clear the stop file and failure counter")
    add("stop", cmd_stop, "ask the orchestrator to end after the current topic")
    add("render", cmd_render, "rewrite the Markdown views")
    p = add(
        "install", cmd_install, "operator: install the OpenCode agents and commands"
    )
    p.add_argument(
        "--target", default=None, help="project directory (default: this repository)"
    )
    p.add_argument(
        "--model", action="append", default=[], metavar="AGENT=PROVIDER/MODEL"
    )
    p.add_argument(
        "--harden", action="store_true", help="also write hard-deny policies"
    )
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--uninstall", action="store_true")
    add("doctor", cmd_doctor, "check the installation")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except ValueError as exc:  # bad slug and similar input errors
        sys.exit(f"ERROR: {exc}")


if __name__ == "__main__":
    main()
