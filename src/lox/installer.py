"""Install the lox agents and commands into a project's ``.opencode`` directory.

The source of truth is ``src/lox/opencode``. Installing copies the Markdown
files and merges three settings into ``.opencode/opencode.json``: nested
subagents (the orchestrator's lead launches its own subagents), automatic
compaction, and, with ``--harden``, hard-deny policies for a dedicated machine.
Providers, models and MCP servers are left alone.
"""

from __future__ import annotations

import datetime as dt
import grp
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from bagel.config import ROOT
from bagel.config import settings as bagel_settings
from lox import store
from lox.settings import load_settings

SOURCE = Path(__file__).with_name("opencode")

# Hard denies for a machine dedicated to lox. They apply to every agent in the
# project, including `build`, so they are opt-in (`--harden`).
POLICIES = [
    f"shell:{command} *"
    for command in (
        "sudo",
        "su",
        "rm",
        "rmdir",
        "mv",
        "dd",
        "chmod",
        "chown",
        "curl",
        "wget",
        "ssh",
        "scp",
        "nc",
        "pip",
        "pip3",
        "uv add",
        "uv remove",
        "uv pip",
        "uv tool",
        "npm",
        "npx",
        "brew",
        "apt",
        "apt-get",
        "git push",
        "git reset",
        "git clean",
        "git checkout",
        "launchctl",
        "systemctl",
        "crontab",
        "kill",
        "pkill",
        "osascript",
    )  # fmt: skip
] + [
    "external_directory:*",
    "edit:src/*",
    "edit:.opencode/*",
    "edit:.git/*",
    "edit:data/*",
    "edit:pyproject.toml",
    "edit:uv.lock",
    "edit:*.env",
    "edit:*.env.*",
    "read:*.env",
    "webfetch:*ssrn.com*",
]


def _with_model(text: str, model: str) -> str:
    """Add a ``model:`` line to an agent's frontmatter."""
    head, sep, rest = text.partition("\nmode:")
    return f"{head}\nmodel: {model}{sep}{rest}" if sep else text


def _models(pairs: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in pairs:
        agent, _, model = pair.partition("=")
        if not agent or "/" not in model:
            sys.exit(f"--model wants AGENT=PROVIDER/MODEL, got {pair!r}")
        out[agent if agent == "all" or agent.startswith("lox") else f"lox-{agent}"] = (
            model
        )
    return out


def _merge_config(path: Path, harden: bool, dry_run: bool) -> None:
    jsonc = path.with_suffix(".jsonc")
    if jsonc.exists():
        print(
            f"! {jsonc} exists and may hold comments, so it was not edited. Add these by hand:\n"
            '    "experimental": {"subagent_depth": 2}, "compaction": {"auto": true}'
        )
        return
    config: dict = {}
    if path.exists():
        try:
            config = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            sys.exit(
                f"{path} is not valid JSON ({exc}); fix it and run the install again"
            )
    before = json.dumps(config, sort_keys=True)

    config.setdefault("$schema", "https://opencode.ai/config.json")
    experimental = config.setdefault("experimental", {})
    experimental["subagent_depth"] = max(int(experimental.get("subagent_depth", 1)), 2)
    config.setdefault("compaction", {}).setdefault("auto", True)
    if harden:
        policies = config.setdefault("policies", [])
        present = {(p.get("action"), p.get("resource")) for p in policies}
        for resource in POLICIES:
            if ("tool.use", resource) not in present:
                policies.append(
                    {"action": "tool.use", "resource": resource, "effect": "deny"}
                )

    if json.dumps(config, sort_keys=True) == before:
        print(f"= {path} already has the lox settings")
        return
    print(
        f"~ {path}: subagent_depth=2, compaction.auto"
        + (", hard-deny policies" if harden else "")
    )
    if dry_run:
        return
    if path.exists():
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(path, path.with_name(f"{path.name}.lox-backup-{stamp}"))
    store.write_text_atomic(path, json.dumps(config, indent=2) + "\n")


def install(target: Path | None, args) -> None:
    root = (target or ROOT).resolve()
    base = root / ".opencode"
    models = _models(args.model)

    for kind in ("agents", "commands"):
        for source in sorted((SOURCE / kind).glob("*.md")):
            dest = base / kind / source.name
            if args.uninstall:
                if dest.exists():
                    print(f"- {dest}")
                    if not args.dry_run:
                        dest.unlink()
                continue
            text = source.read_text()
            model = models.get(source.stem) or models.get("all")
            if kind == "agents" and model:
                text = _with_model(text, model)
            changed = not dest.exists() or dest.read_text() != text
            print(f"{'+' if changed else '='} {dest}")
            if changed and not args.dry_run:
                store.write_text_atomic(dest, text)
    if args.uninstall:
        print(
            "Removed the lox agents and commands. opencode.json and research/ were left alone."
        )
        return

    _merge_config(base / "opencode.json", args.harden, args.dry_run)
    print(
        "\nDone." if not args.dry_run else "\nDry run: nothing was written.",
        "Start OpenCode in",
        root,
        "and run /lox-run, /lox-scout, /lox-one <slug> or /lox-status.",
    )


def doctor() -> int:
    """Print a checklist. Returns 1 when something required is missing."""
    failed = False

    def check(ok: bool, label: str, hint: str = "", required: bool = True) -> None:
        nonlocal failed
        mark = "ok  " if ok else ("FAIL" if required else "warn")
        print(f"[{mark}] {label}" + (f"\n       {hint}" if hint and not ok else ""))
        failed = failed or (required and not ok)

    from lox.engine import inventory

    settings = load_settings()
    rows = [r for r in inventory() if r["tradable"]]
    check(bool(rows), f"data catalog at {bagel_settings.catalog_path} ({len(rows)} usable series)",
          "copy data/catalog from another machine, or run `uv run bagel-download --timeframe all`")  # fmt: skip
    minute = {r["symbol"] for r in rows if r["timeframe"] == "minute"}
    check(
        bool(minute),
        f"1-minute bars for {len(minute)} symbols",
        "intraday topics need them",
    )

    research = store.research_dir()
    check(
        (research / "backlog.json").exists(),
        f"research directory at {research}",
        "run `uv run lox init`",
    )

    opencode = shutil.which("opencode")
    version = ""
    if opencode:
        done = subprocess.run(
            [opencode, "--version"], capture_output=True, text=True, check=False
        )
        version = done.stdout.strip().lstrip("opencode").strip().lstrip("v")
    numbers = tuple(int(n) for n in version.split(".")[:2] if n.isdigit())
    check(bool(opencode) and numbers >= (2, 3), f"opencode {version or 'not found'} (needs 2.3 or newer)",
          "install or upgrade OpenCode")  # fmt: skip

    agents = ROOT / ".opencode" / "agents"
    wanted = [p.name for p in (SOURCE / "agents").glob("*.md")]
    missing = [n for n in wanted if not (agents / n).exists()]
    check(not missing, f"{len(wanted) - len(missing)}/{len(wanted)} lox agents installed in {agents}",
          "run `src/lox/install.sh` (or `uv run lox install`)")  # fmt: skip
    stale = [
        n for n in wanted
        if (agents / n).exists()
        and "\nmodel:" not in (agents / n).read_text()
        and (agents / n).read_text() != (SOURCE / "agents" / n).read_text()
    ]  # fmt: skip
    check(
        not stale,
        "installed agents match src/lox/opencode",
        f"out of date: {', '.join(stale)}; reinstall",
        False,
    )

    config_path = ROOT / ".opencode" / "opencode.json"
    depth = 1
    if config_path.exists():
        try:
            depth = (
                json.loads(config_path.read_text())
                .get("experimental", {})
                .get("subagent_depth", 1)
            )
        except json.JSONDecodeError:
            depth = 0
    check(depth >= 2, "experimental.subagent_depth is 2 or more",
          "the strategy lead cannot launch its phase subagents without it; run the install")  # fmt: skip

    git = settings.section("git")
    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    check(branch == git["branch"], f"on branch '{branch}' (results are committed only on '{git['branch']}')",
          f"`git switch -c {git['branch']}` on the research machine; until then results stay uncommitted", False)  # fmt: skip
    check(
        not git["push"] or branch == git["branch"],
        f"push is {'on' if git['push'] else 'off'}",
        required=False,
    )

    groups = {grp.getgrgid(g).gr_name for g in os.getgroups()}
    privileged = os.geteuid() == 0 or bool(groups & {"admin", "sudo", "wheel"})
    check(not privileged, "running as a non-admin user",
          "this user can administer the machine; run unattended research as a dedicated non-admin user", False)  # fmt: skip
    free = shutil.disk_usage(ROOT).free / 1e9
    floor = settings.section("pipeline")["min_free_disk_gb"]
    check(free >= floor, f"{free:.0f} GB of disk free (floor {floor} GB)")
    return 1 if failed else 0
