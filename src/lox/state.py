"""Four human-readable lists and a tiny per-topic checkpoint."""

import re
from datetime import UTC, datetime

from lox import sweep
from lox.common import (
    RESEARCH,
    atomic_write,
    fingerprint,
    lock,
    owner,
    ready,
    settings,
    topic,
)

PHASES = ("research", "code", "review", "backtest", "analyze", "record")


def backlog():
    return re.findall(
        r"^- \[([ x])\] ([a-z][a-z0-9_]*) \| (.+)$",
        (RESEARCH / "backlog.md").read_text(),
        re.MULTILINE,
    )


def phase(slug):
    return (topic(slug) / "phase").read_text().strip()


def active():
    path = RESEARCH / ".active"
    return path.read_text().splitlines() if path.exists() else []


def require_active(slug):
    if not active() or active()[0] != slug:
        raise ValueError(f"{slug} is not the active topic")


def claim(slug=None):
    ready()
    with lock():
        current = active()
        if current and phase(current[0]) in ("completed", "blocked", "failed"):
            (RESEARCH / ".active").unlink()
            current = []
        if current:
            if current[1] == "released":
                atomic_write(RESEARCH / ".active", f"{current[0]}\n{owner()}\n")
                current[1] = owner()
            if current[1] != owner():
                raise ValueError(
                    f"{current[0]} is owned by another session; use manual `lox resume` after stopping it"
                )
            if slug and slug != current[0]:
                raise ValueError(f"Finish active topic {current[0]} first")
            return current[0]
        failures = re.findall(
            r"^Outcome: (.+)$", (RESEARCH / "researched.md").read_text(), re.MULTILINE
        )
        if failures[-3:] == ["failed"] * 3:
            raise ValueError(
                "Three consecutive failed topics; inspect and resume a topic manually"
            )
        pending = [s for checked, s, _ in backlog() if checked == " "]
        if slug is None:
            slug = next(iter(pending), None)
        if slug is None:
            return "EMPTY"
        if slug not in pending:
            raise ValueError("Topic is not pending in backlog.md")
        folder = topic(slug)
        folder.mkdir(parents=True, exist_ok=True)
        if not (folder / "phase").exists():
            atomic_write(folder / "phase", "research\n")
        atomic_write(RESEARCH / ".active", f"{slug}\n{owner()}\n")
        return slug


def advance(slug):
    ready()
    with lock():
        require_active(slug)
        current = phase(slug)
        folder = topic(slug)
        if current == "research":
            brief = (folder / "brief.md").read_text()
            if not brief.startswith("READY\n") or len(brief) < 200:
                raise ValueError(
                    "Research brief must start READY and explain rules, variations, sources and data"
                )
        elif current == "code":
            if (folder / "checked.sha256").read_text().strip() != fingerprint(slug):
                raise ValueError("Run lox check after coding")
        elif current == "review":
            sweep.reviewed(slug)
        elif current == "backtest":
            sweep.summary(slug)
        elif current == "analyze":
            report = (folder / "report.md").read_text()
            if f"Fingerprint: {fingerprint(slug)}" not in report or len(report) < 200:
                raise ValueError(
                    "Analysis must reference this run's fingerprint and discuss its results"
                )
        else:
            raise ValueError("Use lox record")
        following = PHASES[PHASES.index(current) + 1]
        atomic_write(folder / "phase", following + "\n")
        return following


def repair(slug):
    with lock():
        require_active(slug)
        if phase(slug) not in ("code", "review", "backtest"):
            raise ValueError(
                "Repair is only available for implementation/review/backtest failures"
            )
        file = topic(slug) / "repairs"
        attempts = int(file.read_text()) if file.exists() else 0
        if attempts >= 2:
            raise ValueError("Two repair rounds exhausted; record failure")
        atomic_write(file, str(attempts + 1))
        atomic_write(topic(slug) / "phase", "code\n")


def append_once(name, slug, body):
    path = RESEARCH / name
    text = path.read_text()
    if f"\n## {slug}\n" not in text:
        atomic_write(path, text.rstrip() + f"\n\n## {slug}\n\n{body}\n")


def finish(slug, outcome="completed", reason=""):
    with lock():
        require_active(slug)
        body = f"Outcome: {outcome}\nDate: {datetime.now(UTC).date()}\nArtifacts: research/topics/{slug}/\n\n"
        if outcome == "completed":
            if phase(slug) not in ("record", "completed"):
                raise ValueError("All five phases must finish before recording")
            body += sweep.summary(slug)
            rows = sweep.read_rows(topic(slug) / fingerprint(slug) / "validation.csv")
            candidates = [
                r
                for r in rows
                if float(r["annual_return"]) > settings()["candidate_annual_return"]
            ]
            if candidates:
                append_once("candidates.md", slug, body)
        else:
            if not reason.strip():
                raise ValueError("Give the concrete blocking/failure reason")
            body += reason
            if outcome == "blocked":
                append_once("data-requests.md", slug, body)
        # Idempotent sections make interrupted multi-file updates safe to retry.
        append_once("researched.md", slug, body)
        path = RESEARCH / "backlog.md"
        atomic_write(
            path, path.read_text().replace(f"- [ ] {slug} |", f"- [x] {slug} |")
        )
        atomic_write(topic(slug) / "phase", outcome + "\n")
        (RESEARCH / ".active").unlink()
        return outcome


def add(slug, description):
    topic(slug)
    if not description.strip() or "\n" in description:
        raise ValueError("Give a single-line topic description")
    with lock():
        if (
            any(s == slug for _, s, _ in backlog())
            or f"\n## {slug}\n" in (RESEARCH / "researched.md").read_text()
        ):
            raise ValueError(
                "Topic already queued/researched; refine it rather than duplicating parameters"
            )
        path = RESEARCH / "backlog.md"
        atomic_write(
            path, path.read_text().rstrip() + f"\n- [ ] {slug} | {description}\n"
        )


def resume(slug):
    """Human recovery only: let the next parent claim an interrupted checkpoint."""
    with lock():
        if active() and active()[0] != slug:
            raise ValueError("Another topic is active")
        checkpoint = topic(slug) / "phase"
        if checkpoint.exists():
            if phase(slug) not in PHASES:
                raise ValueError("Topic is already finished")
        elif slug in [s for checked, s, _ in backlog() if checked == " "]:
            atomic_write(checkpoint, "research\n")
        else:
            raise ValueError("Topic is not pending")
        atomic_write(RESEARCH / ".active", f"{slug}\nreleased\n")
