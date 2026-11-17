"""Commands used by the fixed OpenCode pipeline."""

import argparse
import os
import re
import subprocess
from datetime import UTC, datetime

from lox import state, sweep
from lox.common import RESEARCH, settings


def inventory():
    from nautilus_trader.model import NautilusDataType
    from nautilus_trader.persistence import ParquetDataCatalog

    catalog = ParquetDataCatalog(settings()["catalog"])
    identifiers = sorted(
        {str(i.id) for i in catalog.instruments() if str(i.id).endswith(".ARCA")}
    )
    for iid in identifiers:
        for spec in ("1-MINUTE", "1-DAY"):
            series = f"{iid}-{spec}-LAST-EXTERNAL"
            first = catalog.query_first_timestamp(NautilusDataType.Bar, series)
            last = catalog.query_last_timestamp(NautilusDataType.Bar, series)
            dates = [
                datetime.fromtimestamp(t / 1e9, UTC).isoformat() if t else "missing"
                for t in (first, last)
            ]
            print(f"{series}: {dates[0]} through {dates[1]}")


def compact():
    session = os.environ.get("OPENCODE_SESSION_ID", "")
    if not re.fullmatch(r"ses_[A-Za-z0-9]+", session):
        print("No OpenCode session ID; automatic compaction remains available.")
        return
    subprocess.run(
        ["opencode", "api", "post", f"/api/session/{session}/compact", "--data", "{}"],
        check=True,
        timeout=30,
        capture_output=True,
    )
    print("Compaction requested for the next safe point.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "inventory", "compact"):
        sub.add_parser(name)
    for name in ("claim",):
        sub.add_parser(name).add_argument("slug", nargs="?")
    for name in (
        "next",
        "advance",
        "repair",
        "record",
        "check",
        "sweep",
        "results",
        "resume",
    ):
        sub.add_parser(name).add_argument("slug")
    for name in ("block", "fail", "add"):
        command = sub.add_parser(name)
        command.add_argument("slug")
        command.add_argument("reason", help="Quoted description or concrete reason")
    args = parser.parse_args()
    try:
        match args.command:
            case "status":
                print("STOP" if (RESEARCH / "STOP").exists() else "Ready")
                print("Active:", state.active() or "none")
                for checked, slug, description in state.backlog():
                    if checked == " ":
                        print(f"{slug}: {description}")
            case "inventory":
                inventory()
            case "compact":
                compact()
            case "claim":
                print(state.claim(args.slug))
            case "next":
                if (RESEARCH / "STOP").exists():
                    print("STOP")
                else:
                    state.require_active(args.slug)
                    phase = state.phase(args.slug)
                    print(
                        f"{phase}: "
                        + (f"launch lox-{phase}" if phase != "record" else "lox record")
                    )
            case "advance":
                print(state.advance(args.slug))
            case "repair":
                state.repair(args.slug)
                print("Return to lox-code, then lox-review.")
            case "record":
                print(state.finish(args.slug))
            case "block" | "fail":
                print(
                    state.finish(
                        args.slug,
                        "blocked" if args.command == "block" else "failed",
                        args.reason,
                    )
                )
            case "add":
                state.add(args.slug, args.reason)
            case "resume":
                state.resume(args.slug)
            case "check":
                print(sweep.check(args.slug))
            case "sweep":
                print(sweep.run(args.slug))
            case "results":
                print(sweep.summary(args.slug))
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"Lox: {exc}\n")


if __name__ == "__main__":
    main()
