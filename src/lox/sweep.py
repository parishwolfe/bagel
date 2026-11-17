"""Bounded parallel batches; training chooses parameters, validation only evaluates."""

import ast
import csv
import io
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

from lox.common import (
    ROOT,
    atomic_write,
    fingerprint,
    lock,
    ready,
    settings,
    strategy_path,
    topic,
)


def isolated(job):
    env = os.environ.copy()
    env.update(
        OMP_NUM_THREADS="1",
        OPENBLAS_NUM_THREADS="1",
        MKL_NUM_THREADS="1",
        PYTHONDONTWRITEBYTECODE="1",
    )
    try:
        run = subprocess.run(
            [sys.executable, "-m", "lox.worker"],
            input=json.dumps(job),
            capture_output=True,
            text=True,
            cwd=ROOT,
            env=env,
            timeout=job["config"]["timeout_seconds"],
        )
    except subprocess.TimeoutExpired:
        raise ValueError("Backtest worker exceeded wall-clock timeout") from None
    try:
        result = json.loads(run.stdout)
    except json.JSONDecodeError:
        raise ValueError(
            f"Backtest worker exited {run.returncode} without a result"
        ) from None
    if run.returncode or "error" in result:
        raise ValueError(result.get("error", f"Worker exited {run.returncode}"))
    return result


def job_for(slug, **options):
    return dict(path=str(strategy_path(slug)), config=settings(), **options)


def describe(slug):
    return isolated(job_for(slug, action="describe"))


def read_rows(path):
    if not path.exists():
        return []
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def write_rows(path, rows):
    if not rows:
        raise ValueError("No results to write")
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    atomic_write(path, out.getvalue())


def trial(slug, symbol, params, window, stress=False):
    return dict(
        symbol=symbol,
        parameters=repr(params),
        **isolated(
            job_for(
                slug,
                action="run",
                symbol=symbol,
                params=params,
                window=window,
                stress=stress,
            )
        ),
    )


def check(slug):
    """One smoke run per variation and symbol, all grid entries constructor-checked."""
    ready()
    with lock():
        info = describe(slug)
        samples = {}
        for params in info["grid"]:
            samples.setdefault(params["variation"], params)
        rows = []
        for symbol in info["symbols"]:
            for params in samples.values():
                ready()
                rows.append(trial(slug, symbol, params, "smoke"))
        write_rows(topic(slug) / "smoke.csv", rows)
        atomic_write(topic(slug) / "checked.sha256", fingerprint(slug) + "\n")
        return f"{len(info['grid'])} configurations; {len(rows)} smoke runs passed\nFingerprint: {fingerprint(slug)}"


def reviewed(slug):
    folder = topic(slug)
    digest = fingerprint(slug)
    if (folder / "checked.sha256").read_text().strip() != digest:
        raise ValueError("Strategy/settings changed; repeat code check and review")
    review = (folder / "review.md").read_text()
    if not review.startswith("PASS\n") or f"Fingerprint: {digest}" not in review:
        raise ValueError("Need PASS review of the checked fingerprint")
    return digest


def run(slug):
    ready()
    with lock():
        digest = reviewed(slug)
        info = describe(slug)
        folder = topic(slug) / digest
        folder.mkdir(parents=True, exist_ok=True)
        train_path = folder / "train.csv"
        rows = read_rows(train_path)
        done = {(r["symbol"], r["parameters"]) for r in rows}
        work = [
            (s, p)
            for s in info["symbols"]
            for p in info["grid"]
            if (s, repr(p)) not in done
        ]
        config = settings()
        workers = max(
            1, min(int(config["workers"]), max(1, (os.cpu_count() or 1) - 2), 8)
        )
        # Only one worker-sized batch is submitted at a time: STOP is checked each batch.
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for offset in range(0, len(work), workers):
                ready()
                batch = work[offset : offset + workers]
                futures = [pool.submit(trial, slug, s, p, "train") for s, p in batch]
                for future in futures:
                    rows.append(
                        future.result()
                    )  # Fail closed; partial CSV remains resumable.
                    write_rows(train_path, rows)
                print(
                    f"Training: {len(rows)}/{len(info['grid']) * len(info['symbols'])}",
                    flush=True,
                )
        ranked = sorted(rows, key=lambda r: float(r["annual_return"]), reverse=True)
        finalists = ranked[: config["validation_finalists"]]
        validation = []
        for selected in finalists:
            ready()
            params = ast.literal_eval(selected["parameters"])
            base = trial(slug, selected["symbol"], params, "validation")
            ready()
            stress = trial(slug, selected["symbol"], params, "validation", stress=True)
            neighbours = [
                r
                for r in rows
                if r["symbol"] == selected["symbol"]
                and sum(
                    params[k] != v for k, v in ast.literal_eval(r["parameters"]).items()
                )
                == 1
            ]
            base.update(
                training_annual_return=float(selected["annual_return"]),
                stress_annual_return=stress["annual_return"],
                profitable_neighbours=sum(
                    float(r["annual_return"]) > 0 for r in neighbours
                ),
                neighbours=len(neighbours),
            )
            validation.append(base)
        # No tuning using validation results. All finalists were fixed by training.
        if reviewed(slug) != digest:
            raise ValueError(
                "Code/settings changed during sweep; results cannot be recorded"
            )
        write_rows(folder / "validation.csv", validation)
        atomic_write(topic(slug) / "swept.sha256", digest + "\n")
        return summary(slug)


def summary(slug):
    digest = reviewed(slug)
    folder = topic(slug) / digest
    if (topic(slug) / "swept.sha256").read_text().strip() != digest:
        raise ValueError("Sweep does not match current code/settings")
    train = read_rows(folder / "train.csv")
    validation = read_rows(folder / "validation.csv")
    if not train or not validation:
        raise ValueError("Incomplete sweep")
    text = [
        f"Fingerprint: {digest}",
        f"Training trials: {len(train)}",
        f"Validation finalists: {len(validation)}",
        "",
    ]
    for row in validation:
        text.extend(
            [
                f"### {row['symbol']} — {row['parameters']}",
                f"- Validation annual return: {float(row['annual_return']):.2%}",
                f"- Total return: {float(row['return_pct']):.2f}%",
                f"- Closed trades: {row['trades']}",
                f"- Daily-close drawdown: {float(row['max_drawdown']):.2%}",
                f"- Buy-and-hold annual return (before costs): {float(row['benchmark_annual_return']):.2%}",
                f"- Fee-stress annual return: {float(row['stress_annual_return']):.2%}",
                f"- Profitable training neighbours: {row['profitable_neighbours']}/{row['neighbours']}",
                "",
            ]
        )
    return "\n".join(text)
