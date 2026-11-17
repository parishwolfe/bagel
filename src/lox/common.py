"""Small shared file helpers; no broker settings or .env loading."""

import fcntl
import hashlib
import math
import os
import re
import shutil
import tomllib
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RESEARCH = ROOT / "research"
SETTINGS = Path(__file__).with_name("settings.toml")


def settings():
    config = tomllib.loads(SETTINGS.read_text())
    config["catalog"] = str((ROOT / config["catalog"]).resolve())
    for key in ("capital", "workers", "max_trials", "timeout_seconds", "memory_mb"):
        if config[key] <= 0:
            raise ValueError(f"{key} must be positive")
    if not 50 <= config["max_trials"] <= 10000:
        raise ValueError("max_trials must be between 50 and 10000")
    for key in ("commission_per_order", "stress_commission_per_order", "min_free_mb"):
        if not math.isfinite(config[key]) or config[key] < 0:
            raise ValueError(f"{key} must be finite and nonnegative")
    if not 1 <= config["validation_finalists"] <= 10:
        raise ValueError("validation_finalists must be between 1 and 10")
    if config["stress_commission_per_order"] <= config["commission_per_order"]:
        raise ValueError("Stress commission must exceed the base commission")
    return config


def topic(slug):
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", slug):
        raise ValueError("Use a lowercase Python module name for the topic slug")
    return RESEARCH / "topics" / slug


def strategy_path(slug):
    topic(slug)
    return ROOT / "src" / "bagel" / "strategies" / f"{slug}.py"


def atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text)
    temporary.replace(path)


@contextmanager
def lock(name=".lock"):
    RESEARCH.mkdir(parents=True, exist_ok=True)
    with (RESEARCH / name).open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another Lox operation is running") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def ready():
    if (RESEARCH / "STOP").exists():
        raise ValueError("STOP requested; remove research/STOP manually to resume")
    if shutil.disk_usage(ROOT).free < settings()["min_free_mb"] * 1024**2:
        raise ValueError("Disk-free floor reached")


def fingerprint(slug):
    """Invalidate results on code/settings changes or a replaced catalog file."""
    paths = [strategy_path(slug), SETTINGS, Path(__file__).with_name("worker.py")]
    paths.append(Path(__file__).with_name("sweep.py"))
    digest = hashlib.sha256(b"".join(p.read_bytes() for p in paths))
    # Stat metadata only; never query vault bars for a fingerprint.
    catalog = Path(settings()["catalog"]) / "data"
    for kind in ("bars", "equity"):
        for file in sorted((catalog / kind).glob("*.ARCA*/*.parquet")):
            stat = file.stat()
            digest.update(
                f"{file.relative_to(catalog)}:{stat.st_size}:{stat.st_mtime_ns}".encode()
            )
    return digest.hexdigest()


def owner():
    return os.environ.get("OPENCODE_SESSION_ID", "manual")
