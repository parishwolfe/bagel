"""The scouting pass: a fixed set of lanes, an inbox, and a coverage table.

Harvest agents drop raw ideas into the inbox with ``lox scout propose``. The
triage agent turns each one into a backlog topic (or a data request) with
``lox backlog add``, which de-duplicates and prioritises deterministically.
"""

from __future__ import annotations

from lox import store

# lane -> what the harvest agent is told to look for. Intraday first.
LANES = {
    "academic-intraday": (
        "Academic papers on intraday and scalping strategies in liquid US ETFs that can be "
        "tested with 1-minute OHLCV bars. Search SSRN with `site:papers.ssrn.com <keywords>` "
        "and read only the abstracts in the search results (never fetch ssrn.com pages), then "
        "look for the same paper or idea on arXiv q-fin, NBER, author pages and blog write-ups."
    ),
    "practitioner-intraday": (
        "Practitioner intraday and scalping setups with explicit, mechanical rules: quant "
        "blogs, strategy libraries, backtest write-ups and forums. Skip anything that is a "
        "feeling rather than a rule."
    ),
    "price-action": (
        "Price-action and market-structure setups that can be written as exact rules on "
        "1-minute bars: liquidity sweeps, fair value gaps, session ranges, failed breakouts, "
        "VWAP reactions, opening and closing auction effects."
    ),
    "internal": (
        "No web search. Run `uv run lox ledger` and read the reports of past topics under "
        "research/topics/*/report.md. Propose follow-ups: variations of 'candidate' and "
        "'watch' topics, ideas named in a report's Follow-ups section, and combinations of "
        "two things that each half-worked."
    ),
    "swing-daily": (
        "Daily-bar strategies for the same ETFs (pairs, rotation, trend, mean reversion, "
        "calendar effects). These are deliberately low priority: propose only the strongest."
    ),
}


def _inbox():
    return store.research_dir() / "scout" / "inbox.jsonl"


def propose(
    *, lane: str, title: str, hypothesis: str, rules: str, data: str, sources: list[str]
) -> int:
    if lane not in LANES:
        raise ValueError(f"lane must be one of {', '.join(LANES)}")
    row = {
        "lane": lane,
        "title": title.strip(),
        "hypothesis": hypothesis.strip(),
        "rules": rules.strip(),
        "data": data.strip(),
        "sources": sources,
        "proposed_at": store.now(),
    }
    with store.locked():
        store.append_jsonl(_inbox(), row)
        return len(store.read_jsonl(_inbox()))


def inbox() -> list[dict]:
    return store.read_jsonl(_inbox())


def end() -> int:
    """Archive the inbox. Returns how many proposals it held."""
    path = _inbox()
    rows = store.read_jsonl(path)
    if path.exists():
        stamp = store.now().replace(":", "").replace("-", "")
        path.rename(path.with_name(f"inbox-{stamp}.jsonl"))
    return len(rows)


def coverage() -> str:
    """Style x horizon counts across the backlog and the ledger."""
    horizons = list(store.HORIZON_PRIORITY)
    counts = {style: dict.fromkeys(horizons, 0) for style in store.STYLES}
    for card in store.known_topics():
        style, horizon = card.get("style"), card.get("horizon")
        if style in counts and horizon in counts[style]:
            counts[style][horizon] += 1
    return store._table(
        ["Style", *horizons],
        [[style, *(counts[style][h] for h in horizons)] for style in store.STYLES],
    )
