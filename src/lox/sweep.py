"""Parameter sweeps and held-out validation for one topic.

``spec.json`` (written by the design agent) lists the variations and, for each,
a grid of parameter values. ``run_sweep`` backtests every combination on the
train window in parallel worker processes; it is resumable and stops after a
time budget so one call always fits inside a tool timeout. ``validate`` then
reruns the best configurations on the held-out window and applies the flag
rule.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import multiprocessing as mp
import os
import random
import re
import time
from pathlib import Path

from lox import store
from lox.engine import run_backtest, tradable_symbols
from lox.settings import load_settings

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,39}\Z")


# --- spec ---------------------------------------------------------------------


def spec_path(slug: str) -> Path:
    return store.topic_dir(slug) / "spec.json"


def strategy_path(slug: str) -> Path:
    return store.topic_dir(slug) / "strategy.py"


def load_spec(slug: str) -> tuple[dict | None, list[str]]:
    """Parse and validate ``spec.json``. Returns (spec, problems)."""
    path = spec_path(slug)
    if not path.exists():
        return None, [f"{path} does not exist"]
    try:
        spec = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        return None, [f"spec.json is not valid JSON: {exc}"]
    return spec, validate_spec(spec)


def validate_spec(spec: dict) -> list[str]:
    limits = load_settings().section("sweep")
    problems: list[str] = []
    if not isinstance(spec, dict):
        return ["spec.json must be a JSON object"]

    timeframe = spec.get("timeframe")
    if timeframe not in ("minute", "daily"):
        return ['"timeframe" must be "minute" or "daily"']
    if spec.get("mode", "single") not in ("single", "basket"):
        problems.append(
            '"mode" must be "single" (one run per symbol) or "basket" (all symbols in one run)'
        )
    warmup = spec.get("warmup_days", 0)
    if not isinstance(warmup, int) or not 0 <= warmup <= 400:
        problems.append('"warmup_days" must be an integer from 0 to 400')

    symbols = spec.get("symbols")
    available = tradable_symbols(timeframe)
    if not isinstance(symbols, list) or not symbols:
        problems.append('"symbols" must be a non-empty list')
    else:
        missing = [s for s in symbols if s not in available]
        if missing:
            problems.append(
                f"no {timeframe} data for {', '.join(map(str, missing))}; "
                f"available: {', '.join(sorted(available))}"
            )
        if spec.get("mode") == "basket" and len(symbols) < 2:
            problems.append('"basket" mode needs at least two symbols')

    variants = spec.get("variants")
    if not isinstance(variants, list) or len(variants) < limits["min_variants"]:
        problems.append(
            f'"variants" must list at least {limits["min_variants"]} variations'
        )
        return problems
    seen: set[str] = set()
    for i, variant in enumerate(variants):
        where = f"variants[{i}]"
        if not isinstance(variant, dict):
            problems.append(f"{where} must be an object")
            continue
        name = variant.get("name")
        if not isinstance(name, str) or not _NAME.match(name):
            problems.append(
                f'{where}: "name" must be lowercase letters, digits and dashes'
            )
        elif name in seen:
            problems.append(f'{where}: duplicate name "{name}"')
        else:
            seen.add(name)
        if not isinstance(variant.get("class"), str) or not _IDENT.match(
            variant["class"]
        ):
            problems.append(f'{where}: "class" must be a Python class name')
        if not str(variant.get("description", "")).strip():
            problems.append(f'{where}: "description" is required')
        grid = variant.get("grid")
        if not isinstance(grid, dict) or not grid:
            problems.append(
                f'{where}: "grid" must map parameter names to lists of values'
            )
            continue
        combos = 1
        for key, values in grid.items():
            ok = (
                isinstance(values, list)
                and values
                and all(isinstance(v, (int, float, str, bool)) for v in values)
            )
            if not _IDENT.match(str(key)) or not ok:
                problems.append(
                    f"{where}: grid[{key!r}] must be a non-empty list of numbers or strings"
                )
            elif len(set(map(json.dumps, values))) != len(values):
                problems.append(f"{where}: grid[{key!r}] repeats a value")
            else:
                combos *= len(values)
        if combos < limits["min_runs_per_variant"]:
            problems.append(
                f"{where}: the grid has {combos} combination(s); "
                f"give at least {limits['min_runs_per_variant']}"
            )
    return problems


def run_key(variant: str, symbols: list[str], params: dict) -> str:
    blob = json.dumps([variant, symbols, params], sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()[:16]


def expand(slug: str, spec: dict) -> list[dict]:
    """Every (variant, symbols, parameters) run, capped by a reproducible sample."""
    groups = (
        [spec["symbols"]]
        if spec.get("mode") == "basket"
        else [[s] for s in spec["symbols"]]
    )
    runs: list[dict] = []
    for variant in spec["variants"]:
        keys = sorted(variant["grid"])
        for values in itertools.product(*(variant["grid"][k] for k in keys)):
            params = dict(zip(keys, values, strict=True))
            for symbols in groups:
                runs.append(
                    {
                        "key": run_key(variant["name"], symbols, params),
                        "variant": variant["name"],
                        "class": variant["class"],
                        "symbols": symbols,
                        "params": params,
                    }
                )
    cap = int(load_settings().section("sweep")["max_runs"])
    if len(runs) > cap:
        keep = set(random.Random(slug).sample(range(len(runs)), cap))
        runs = [r for i, r in enumerate(runs) if i in keep]
    return runs


# --- workers ---------------------------------------------------------------------


def _init_worker(memory_limit_mb: int) -> None:
    os.nice(10)
    try:  # enforced on Linux; macOS ignores or rejects address-space limits
        import resource

        limit = memory_limit_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except ImportError, ValueError, OSError:
        pass


def _execute(task: dict) -> dict:
    result = run_backtest(
        strategy_path=Path(task["strategy_path"]),
        class_name=task["class"],
        params=task["params"],
        symbols=task["symbols"],
        timeframe=task["timeframe"],
        window=task["window"],
        costs=task["costs"],
        warmup_days=task["warmup_days"],
        timeout=task["timeout"],
    )
    row = {
        k: task[k]
        for k in ("key", "variant", "class", "symbols", "params", "window", "costs")
    }
    row.update(result)
    if row.get("error"):
        row["error"] = row["error"][:1500]
    return row


def _task(slug: str, spec: dict, run: dict, window: str, costs: str) -> dict:
    return {
        **run,
        "strategy_path": str(strategy_path(slug)),
        "timeframe": spec["timeframe"],
        "warmup_days": int(spec.get("warmup_days", 0)),
        "window": window,
        "costs": costs,
        "timeout": float(load_settings().section("sweep")["run_timeout_seconds"]),
    }


def results_path(slug: str, name: str) -> Path:
    return store.topic_dir(slug) / "results" / f"{name}.jsonl"


def workers() -> int:
    configured = int(load_settings().section("sweep")["max_workers"])
    return configured if configured > 0 else max((os.cpu_count() or 2) - 2, 1)


def run_sweep(slug: str, spec: dict, budget_seconds: float | None = None) -> dict:
    """Run pending train-window backtests until done or the budget is spent."""
    limits = load_settings().section("sweep")
    budget = limits["budget_seconds"] if budget_seconds is None else budget_seconds
    runs = expand(slug, spec)
    path = results_path(slug, "train")
    done = {r["key"] for r in store.read_jsonl(path)}
    wanted = {r["key"] for r in runs}
    pending = [
        _task(slug, spec, r, "train", "base") for r in runs if r["key"] not in done
    ]
    progress = {"total": len(runs), "done": len(done & wanted), "stalled": False}
    if not pending:
        return progress

    started = time.time()
    stall_after = limits["run_timeout_seconds"] + 60
    pool = mp.get_context("spawn").Pool(
        processes=min(workers(), len(pending)),
        initializer=_init_worker,
        initargs=(int(limits["memory_limit_mb"]),),
    )
    try:
        # chunksize=1 keeps the iterator whose next() takes a timeout.
        stream = pool.imap_unordered(_execute, pending, chunksize=1)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as out:
            for _ in pending:
                try:
                    row = stream.next(timeout=stall_after)
                except mp.TimeoutError:
                    progress["stalled"] = True  # a worker hung past every limit
                    break
                out.write(json.dumps(row) + "\n")
                out.flush()
                progress["done"] += 1
                if budget and time.time() - started > budget:
                    break
    finally:
        pool.terminate()
        pool.join()
    return progress


# --- summary ------------------------------------------------------------------------


def _ok(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r.get("status") == "ok"]


def _fmt(row: dict) -> list:
    return [
        row["variant"],
        " ".join(row["symbols"]),
        json.dumps(row["params"]),
        f"{row['annual_return'] * 100:+.1f}%",
        f"{row['benchmark_annual_return'] * 100:+.1f}%",
        f"{row['max_drawdown'] * 100:.1f}%",
        row["sharpe"],
        row["trades"],
    ]


def write_sweep_summary(slug: str, spec: dict) -> dict:
    """Condense the train results into a small JSON + Markdown the agents can read."""
    rows = store.read_jsonl(results_path(slug, "train"))
    good = _ok(rows)
    by_status: dict[str, int] = {}
    for row in rows:
        by_status[row.get("status", "?")] = by_status.get(row.get("status", "?"), 0) + 1

    variants = []
    for variant in spec["variants"]:
        mine = [r for r in good if r["variant"] == variant["name"]]
        returns = sorted(r["annual_return"] for r in mine)
        variants.append(
            {
                "variant": variant["name"],
                "runs": len(mine),
                "profitable_share": round(sum(r > 0 for r in returns) / len(returns), 3)
                if returns
                else 0.0,
                "median_annual_return": returns[len(returns) // 2] if returns else None,
                "best_annual_return": returns[-1] if returns else None,
            }
        )
    top = sorted(good, key=lambda r: r["annual_return"], reverse=True)[:15]
    errors = [r for r in rows if r.get("status") != "ok"][:5]
    summary = {
        "runs": len(rows),
        "by_status": by_status,
        "variants": variants,
        "top": top,
        "sample_errors": [
            {"variant": r["variant"], "status": r["status"], "error": r.get("error")}
            for r in errors
        ],
    }
    folder = store.topic_dir(slug)
    store.write_json(folder / "sweep_summary.json", summary)

    lines = [
        f"# Sweep summary: {slug}",
        "",
        f"Train window, base costs. {len(rows)} runs: "
        + ", ".join(f"{n} {s}" for s, n in sorted(by_status.items()))
        + ".",
        "",
        "## By variation",
        "",
        store._table(
            ["Variation", "Runs", "Profitable", "Median /yr", "Best /yr"],
            [
                [
                    v["variant"],
                    v["runs"],
                    f"{v['profitable_share'] * 100:.0f}%",
                    store._pct(v["median_annual_return"]),
                    store._pct(v["best_annual_return"]),
                ]
                for v in variants
            ],
        ),
        "## Top 15 on the train window",
        "",
        "These were picked on this data, so they overstate the edge. "
        "The validate table is the one that counts.",
        "",
        store._table(
            [
                "Variation",
                "Symbols",
                "Parameters",
                "Return /yr",
                "Buy & hold /yr",
                "Max drawdown",
                "Sharpe",
                "Trades",
            ],
            [_fmt(r) for r in top],
        ),
    ]
    if errors:
        lines += ["## Runs that did not finish cleanly", ""]
        lines += [
            f"- `{e['variant']}` {e['status']}: {str(e.get('error', ''))[:300]}"
            for e in errors
        ]
        lines.append("")
    store.write_text_atomic(folder / "sweep_summary.md", "\n".join(lines))
    return summary


# --- validation and the flag rule ---------------------------------------------------------


def _pick(rows: list[dict], k: int) -> list[dict]:
    """Top-k train configurations, taking the best of each (variant, symbols) first."""
    ranked = sorted(rows, key=lambda r: r["annual_return"], reverse=True)
    chosen: dict[str, dict] = {}
    for row in ranked:  # one per (variant, symbols) so every variation gets a hearing
        group = json.dumps([row["variant"], row["symbols"]])
        if group not in {
            json.dumps([c["variant"], c["symbols"]]) for c in chosen.values()
        }:
            chosen[row["key"]] = row
        if len(chosen) >= k:
            break
    for row in ranked:
        if len(chosen) >= k:
            break
        chosen.setdefault(row["key"], row)
    return list(chosen.values())


def _neighbours(spec: dict, row: dict) -> list[dict]:
    """Parameter sets one grid step away from ``row`` in exactly one parameter."""
    grid = next(v["grid"] for v in spec["variants"] if v["name"] == row["variant"])
    out = []
    for key, values in grid.items():
        try:
            ordered = sorted(values)
        except TypeError:
            ordered = list(values)
        if row["params"].get(key) not in ordered:
            continue
        at = ordered.index(row["params"][key])
        for step in (-1, 1):
            if 0 <= at + step < len(ordered):
                out.append({**row["params"], key: ordered[at + step]})
    return out


def validate(slug: str, spec: dict) -> dict:
    """Rerun the best train configurations on held-out data and apply the flag rule."""
    settings = load_settings()
    rule = settings.section("flag")
    limits = settings.section("sweep")
    train_rows = store.read_jsonl(results_path(slug, "train"))
    train = {r["key"]: r for r in _ok(train_rows)}
    eligible = [
        r for r in train.values() if r["trades"] >= 5 and r["annual_return"] > 0
    ]
    picks = _pick(eligible, int(limits["top_k_validate"]))

    out_path = results_path(slug, "validate")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()

    table = []
    for pick in picks:
        run = {k: pick[k] for k in ("key", "variant", "class", "symbols", "params")}
        held_out = _execute(_task(slug, spec, run, "validate", "base"))
        stress = _execute(_task(slug, spec, run, "validate", "stress"))
        store.append_jsonl(out_path, held_out)
        store.append_jsonl(out_path, stress)

        # Neighbouring parameter sets on the train window: is this a plateau or a spike?
        near = []
        for params in _neighbours(spec, pick):
            key = run_key(pick["variant"], pick["symbols"], params)
            row = train.get(key)
            if row is None:  # sampled out of a capped grid: run it now
                extra = {**run, "key": key, "params": params}
                row = _execute(_task(slug, spec, extra, "train", "base"))
                store.append_jsonl(results_path(slug, "train"), row)
                if row.get("status") == "ok":
                    train[key] = row
            if row.get("status") == "ok":
                near.append(row["total_return"] > 0)
        near_share = sum(near) / len(near) if near else 0.0

        ok = held_out.get("status") == "ok" and stress.get("status") == "ok"
        checks = {
            "ran_cleanly": ok,
            "annual_return": ok
            and held_out["annual_return"] >= rule["min_annual_return"],
            "beats_buy_and_hold": ok
            and (
                not rule["must_beat_buy_and_hold"]
                or held_out["annual_return"] > held_out["benchmark_annual_return"]
            ),
            "enough_trades": ok and held_out["trades"] >= rule["min_trades"],
            "profitable_under_stress": ok
            and (not rule["require_profit_under_stress"] or stress["total_return"] > 0),
            "neighbours_profitable": near_share
            >= rule["min_neighbor_profitable_share"],
        }
        table.append(
            {
                "key": pick["key"],
                "variant": pick["variant"],
                "class": pick["class"],
                "symbols": pick["symbols"],
                "params": pick["params"],
                "train": _brief(pick),
                "validate": _brief(held_out),
                "stress": _brief(stress),
                "neighbours": {
                    "tested": len(near),
                    "profitable_share": round(near_share, 3),
                },
                "checks": checks,
                "passes": all(checks.values()),
            }
        )

    def held_out_return(entry: dict) -> float:
        value = entry["validate"].get("annual_return")
        return value if value is not None else -9.0

    table.sort(key=held_out_return, reverse=True)
    passing = [t for t in table if t["passes"]]
    best = passing[0] if passing else (table[0] if table else None)
    if passing:
        verdict, reason = "candidate", "passed every check on the validate window"
    elif best is None:
        verdict, reason = (
            "rejected",
            "no configuration was profitable on the train window",
        )
    else:
        failed = [
            name.replace("_", " ") for name, ok in best["checks"].items() if not ok
        ]
        v = best["validate"]
        watch = (
            best["checks"]["ran_cleanly"]
            and v["annual_return"] > 0
            and v["annual_return"] > v["benchmark_annual_return"]
            and v["trades"] >= rule["min_trades"]
        )
        verdict = "watch" if watch else "rejected"
        reason = "best configuration failed: " + ", ".join(failed)

    result = {
        "slug": slug,
        "verdict": verdict,
        "reason": reason,
        "rule": dict(rule),
        "trials": len(train_rows),
        "best": best,
        "table": table,
        "validated_at": store.now(),
    }
    store.write_json(store.topic_dir(slug) / "verdict.json", result)
    _write_validation_md(slug, result)
    return result


def _brief(row: dict) -> dict:
    keys = (
        "status", "annual_return", "total_return", "benchmark_annual_return",
        "max_drawdown", "sharpe", "trades", "win_rate", "profit_factor", "max_leverage",
    )  # fmt: skip
    out = {k: row.get(k) for k in keys}
    if row.get("status") != "ok":
        out["error"] = str(row.get("error", ""))[:400]
    return out


def _write_validation_md(slug: str, result: dict) -> None:
    rule = result["rule"]
    lines = [
        f"# Validation: {slug}",
        "",
        f"**Verdict: {result['verdict']}** ({result['reason']}).",
        "",
        f"Flag rule, all on the validate window: at least {rule['min_annual_return'] * 100:.0f}% a year "
        f"after costs, better than buy-and-hold, at least {rule['min_trades']} trades, still profitable "
        "under stress costs, and at least "
        f"{rule['min_neighbor_profitable_share'] * 100:.0f}% of neighbouring parameter sets profitable on train.",
        "",
        f"{result['trials']} configurations were tried on the train window before these were picked.",
        "",
    ]
    rows = []
    for t in result["table"]:
        v, s = t["validate"], t["stress"]
        failed = [n.replace("_", " ") for n, ok in t["checks"].items() if not ok]
        rows.append(
            [
                t["variant"],
                " ".join(t["symbols"]),
                json.dumps(t["params"]),
                store._pct(t["train"]["annual_return"]),
                store._pct(v.get("annual_return")),
                store._pct(v.get("benchmark_annual_return")),
                store._pct(s.get("annual_return")),
                store._pct(v.get("max_drawdown")),
                v.get("trades"),
                f"{t['neighbours']['profitable_share'] * 100:.0f}% of {t['neighbours']['tested']}",
                "PASS" if t["passes"] else "fails: " + ", ".join(failed),
            ]
        )
    lines.append(
        store._table(
            [
                "Variation",
                "Symbols",
                "Parameters",
                "Train /yr",
                "Validate /yr",
                "Buy & hold /yr",
                "Stress /yr",
                "Max drawdown",
                "Trades",
                "Neighbours profitable",
                "Result",
            ],
            rows,
        )  # fmt: skip
    )
    store.write_text_atomic(store.topic_dir(slug) / "validation.md", "\n".join(lines))
