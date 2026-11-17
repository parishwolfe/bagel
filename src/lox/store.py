"""Lox state files: the feedback loop.

Everything lives under ``research/`` and is written only through this module
(atomic writes under one file lock), never by a model editing a file by hand.

    backlog.json         topics still to research
    ledger.jsonl         every topic already researched, with its verdict
    candidates.jsonl     topics that passed the flag rule (follow up by hand)
    data_requests.jsonl  topics that need data we do not have yet

Each has a rendered Markdown view (BACKLOG.md, RESEARCHED.md, CANDIDATES.md,
DATA_REQUESTS.md) for reading.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import json
import os
import re
import tempfile
from pathlib import Path

from lox.settings import load_settings

STYLES = (
    "breakout",
    "momentum",
    "mean-reversion",
    "trend",
    "pairs",
    "price-action",
    "calendar",
    "volatility",
    "rotation",
    "other",
)
# Scalping and intraday come first; longer horizons are backloaded.
HORIZON_PRIORITY = {"scalping": 90, "intraday": 80, "swing": 40, "position": 20}
TIMEFRAMES = ("minute", "daily")
VERDICTS = ("candidate", "watch", "rejected", "needs_data", "failed")

_STOPWORDS = {
    "a",
    "an",
    "and",
    "the",
    "of",
    "on",
    "in",
    "to",
    "for",
    "with",
    "strategy",
    "strategies",
    "trading",
    "bars",
    "bar",
    "using",
    "based",
}


def now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- paths ------------------------------------------------------------------


def research_dir() -> Path:
    return load_settings().research_dir


def topic_dir(slug: str) -> Path:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,63}", slug):
        raise ValueError(f"invalid topic slug: {slug!r}")
    return research_dir() / "topics" / slug


def _path(name: str) -> Path:
    return research_dir() / name


# --- low-level io -------------------------------------------------------------


@contextlib.contextmanager
def locked():
    """Serialise every state change across processes."""
    root = research_dir()
    root.mkdir(parents=True, exist_ok=True)
    with open(root / ".lock", "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def read_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text())


def write_json(path: Path, value) -> None:
    write_text_atomic(path, json.dumps(value, indent=2, sort_keys=False) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as handle:
        handle.write(json.dumps(row, sort_keys=False) + "\n")


# --- topic cards ----------------------------------------------------------------


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:64].strip("-") or "topic"


def _tokens(text: str) -> frozenset[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return frozenset(w for w in words if w not in _STOPWORDS and len(w) > 1)


def _names(card: dict) -> list[str]:
    return [card.get("slug", ""), card.get("title", ""), *card.get("aliases", [])]


def find_duplicate(card: dict, known: list[dict]) -> str | None:
    """Slug of a known topic this card duplicates, if any.

    A match is the same slug, a shared name or alias, or names whose word sets
    overlap by 75% or more (Jaccard).
    """
    mine = [(n, _tokens(n)) for n in _names(card) if n]
    for other in known:
        if other.get("slug") == card.get("slug"):
            return other["slug"]
        for name in _names(other):
            theirs = _tokens(name)
            if not theirs:
                continue
            for _, tokens in mine:
                if not tokens:
                    continue
                overlap = len(tokens & theirs) / len(tokens | theirs)
                if overlap >= 0.75:
                    return other.get("slug")
    return None


def make_card(
    *,
    title: str,
    hypothesis: str,
    style: str,
    horizon: str,
    timeframe: str,
    symbols: list[str],
    data_needed: str = "",
    sources: list[str] | None = None,
    aliases: list[str] | None = None,
    evidence: int = 0,
    priority: int | None = None,
    parent: str | None = None,
    added_by: str = "human",
    slug: str | None = None,
) -> dict:
    if style not in STYLES:
        raise ValueError(f"style must be one of {', '.join(STYLES)}")
    if horizon not in HORIZON_PRIORITY:
        raise ValueError(f"horizon must be one of {', '.join(HORIZON_PRIORITY)}")
    if timeframe not in TIMEFRAMES:
        raise ValueError(f"timeframe must be one of {', '.join(TIMEFRAMES)}")
    if not title.strip() or not hypothesis.strip():
        raise ValueError("title and hypothesis are required")
    evidence = max(0, min(9, int(evidence)))
    return {
        "slug": slug or slugify(title),
        "title": title.strip(),
        "aliases": [a.strip() for a in (aliases or []) if a.strip()],
        "hypothesis": hypothesis.strip(),
        "style": style,
        "horizon": horizon,
        "timeframe": timeframe,
        "symbols": [s.upper() for s in symbols],
        "data_needed": data_needed.strip(),
        "sources": list(sources or []),
        "evidence": evidence,
        "priority": (
            int(priority)
            if priority is not None
            else HORIZON_PRIORITY[horizon] + evidence
        ),
        "status": "ready",
        "parent": parent,
        "added_by": added_by,
        "added_at": now(),
    }


# --- backlog ----------------------------------------------------------------------


def load_backlog() -> list[dict]:
    return read_json(_path("backlog.json"), [])


def _save_backlog(items: list[dict]) -> None:
    items.sort(key=lambda c: (-c["priority"], c["added_at"], c["slug"]))
    write_json(_path("backlog.json"), items)


def known_topics() -> list[dict]:
    """Everything already queued, researched, or parked on missing data."""
    return load_backlog() + load_ledger()


def add_topic(card: dict) -> tuple[str, str]:
    """Add a card. Returns ("added" | "duplicate", slug)."""
    with locked():
        duplicate = find_duplicate(card, known_topics())
        if duplicate:
            return "duplicate", duplicate
        items = load_backlog()
        items.append(card)
        _save_backlog(items)
        render_views()
    return "added", card["slug"]


def get_topic(slug: str) -> dict | None:
    for card in load_backlog():
        if card["slug"] == slug:
            return card
    saved = topic_dir(slug) / "topic.json"
    return read_json(saved, None)


def claim_next(session: str | None = None) -> dict | None:
    """Mark the highest-priority ready topic in progress and return it.

    A topic already in progress is returned again, so an interrupted run
    resumes instead of starting something new.
    """
    with locked():
        items = load_backlog()
        current = next((c for c in items if c["status"] == "in_progress"), None)
        if current is None:
            current = next((c for c in items if c["status"] == "ready"), None)
            if current is None:
                return None
            current["status"] = "in_progress"
            current["claimed_at"] = now()
        current["claimed_by"] = session
        _save_backlog(items)
        write_json(topic_dir(current["slug"]) / "topic.json", current)
        render_views()
        return current


def remove_from_backlog(slug: str) -> dict | None:
    items = load_backlog()
    card = next((c for c in items if c["slug"] == slug), None)
    if card is not None:
        _save_backlog([c for c in items if c["slug"] != slug])
    return card


# --- ledger, candidates, data requests ----------------------------------------------


def load_ledger() -> list[dict]:
    return read_jsonl(_path("ledger.jsonl"))


def load_candidates() -> list[dict]:
    return read_jsonl(_path("candidates.jsonl"))


def load_data_requests() -> list[dict]:
    return read_jsonl(_path("data_requests.jsonl"))


def add_data_request(
    *, slug: str, data: str, why: str, source: str = "", blocking: bool = True
) -> dict:
    row = {
        "slug": slug,
        "data": data.strip(),
        "why": why.strip(),
        "source": source.strip(),
        "blocking": blocking,
        "status": "open",
        "requested_at": now(),
    }
    with locked():
        append_jsonl(_path("data_requests.jsonl"), row)
        render_views()
    return row


def record_result(row: dict, candidate: dict | None) -> None:
    """Move a topic from the backlog to the ledger (and candidates, if flagged)."""
    with locked():
        remove_from_backlog(row["slug"])
        append_jsonl(_path("ledger.jsonl"), row)
        if candidate is not None:
            append_jsonl(_path("candidates.jsonl"), candidate)
        render_views()


def requeue(slug: str) -> dict:
    """Put a researched topic back on the backlog (for example once data arrives)."""
    with locked():
        card = read_json(topic_dir(slug) / "topic.json", None)
        if card is None:
            raise ValueError(f"no saved topic card for {slug}")
        if any(c["slug"] == slug for c in load_backlog()):
            raise ValueError(f"{slug} is already on the backlog")
        card["status"] = "ready"
        card.pop("claimed_at", None)
        card.pop("claimed_by", None)
        rows = [r for r in load_ledger() if r["slug"] != slug]
        write_text_atomic(
            _path("ledger.jsonl"), "".join(json.dumps(r) + "\n" for r in rows)
        )
        items = load_backlog()
        items.append(card)
        _save_backlog(items)
        render_views()
        return card


# --- run-control state ---------------------------------------------------------------


def load_control() -> dict:
    return read_json(_path(".control.json"), {"consecutive_failures": 0})


def save_control(control: dict) -> None:
    write_json(_path(".control.json"), control)


# --- markdown views --------------------------------------------------------------------


def _cell(value) -> str:
    return str(value).replace("|", "/").replace("\n", " ").strip()


def _table(headers: list[str], rows: list[list]) -> str:
    if not rows:
        return "_Nothing here yet._\n"
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(_cell(v) for v in row) + " |" for row in rows]
    return "\n".join(out) + "\n"


def _pct(value) -> str:
    return "" if value is None else f"{value * 100:+.1f}%"


def render_views() -> None:
    """Rewrite the four Markdown views from the machine-readable files."""
    root = research_dir()
    head = "<!-- Generated by lox. Do not edit: change state with the lox CLI. -->\n\n"

    backlog = load_backlog()
    write_text_atomic(
        root / "BACKLOG.md",
        head
        + "# To be researched\n\n"
        + _table(
            ["Priority", "Topic", "Status", "Horizon", "Bars", "Symbols", "Hypothesis"],
            [
                [
                    c["priority"],
                    f"{c['title']} (`{c['slug']}`)",
                    c["status"],
                    c["horizon"],
                    c["timeframe"],
                    " ".join(c["symbols"]),
                    c["hypothesis"],
                ]
                for c in backlog
            ],
        ),
    )

    ledger = load_ledger()
    write_text_atomic(
        root / "RESEARCHED.md",
        head
        + "# Already researched\n\n"
        + _table(
            [
                "Date",
                "Topic",
                "Verdict",
                "Validate /yr",
                "Buy & hold /yr",
                "Trades",
                "Runs",
                "Why",
            ],
            [
                [
                    r["recorded_at"][:10],
                    f"{r['title']} (`{r['slug']}`)",
                    r["verdict"],
                    _pct(r.get("validate_annual_return")),
                    _pct(r.get("benchmark_annual_return")),
                    r.get("validate_trades", ""),
                    r.get("trials", ""),
                    r.get("reason", ""),
                ]
                for r in reversed(ledger)
            ],
        ),
    )

    candidates = load_candidates()
    write_text_atomic(
        root / "CANDIDATES.md",
        head
        + "# Candidates for follow-up\n\n"
        + "Passed the flag rule on held-out data. Nothing here is deployed: "
        + "each needs more testing and paper trading first.\n\n"
        + _table(
            [
                "Date",
                "Topic",
                "Variant",
                "Symbols",
                "Parameters",
                "Validate /yr",
                "Buy & hold /yr",
                "Max drawdown",
                "Trades",
            ],
            [
                [
                    c["recorded_at"][:10],
                    f"{c['title']} (`{c['slug']}`)",
                    c["variant"],
                    " ".join(c["symbols"]),
                    json.dumps(c["params"]),
                    _pct(c["validate"]["annual_return"]),
                    _pct(c["validate"]["benchmark_annual_return"]),
                    _pct(c["validate"]["max_drawdown"]),
                    c["validate"]["trades"],
                ]
                for c in reversed(candidates)
            ],
        ),
    )

    requests = load_data_requests()
    write_text_atomic(
        root / "DATA_REQUESTS.md",
        head
        + "# Data requests\n\n"
        + "Topics that need data the catalog does not have. A blocking request "
        + "parks the topic as `needs_data` instead of calling it non-viable; "
        + "run `lox backlog requeue <slug>` once the data is in.\n\n"
        + _table(
            [
                "Date",
                "Topic",
                "Data needed",
                "Blocks the topic",
                "Possible source",
                "Why",
                "Status",
            ],
            [
                [
                    r["requested_at"][:10],
                    f"`{r['slug']}`",
                    r["data"],
                    "yes" if r["blocking"] else "no (tested in reduced form)",
                    r["source"],
                    r["why"],
                    r["status"],
                ]
                for r in reversed(requests)
            ],
        ),
    )


def init(seed_file: Path) -> list[str]:
    """Create the state directory and load the seed backlog once."""
    root = research_dir()
    (root / "topics").mkdir(parents=True, exist_ok=True)
    added: list[str] = []
    if not _path("backlog.json").exists():
        for seed in json.loads(seed_file.read_text()):
            status, slug = add_topic(make_card(added_by="seed", **seed))
            if status == "added":
                added.append(slug)
    with locked():
        render_views()
    return added
