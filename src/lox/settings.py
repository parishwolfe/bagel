"""Lox settings: defaults from ``settings.toml`` next to this file.

``LOX_SETTINGS`` may name a second TOML file whose keys override the defaults,
and ``LOX_RESEARCH_DIR`` moves the state directory (both are used by the tests).
The file lives under ``src/`` on purpose: agents may only write under
``research/``, so they cannot loosen a threshold.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from bagel.config import ROOT

DEFAULTS = Path(__file__).with_name("settings.toml")


@dataclass(frozen=True)
class Costs:
    commission_per_share: float
    commission_per_order: float
    slippage_ticks: int


@dataclass(frozen=True)
class Windows:
    train_start: str
    train_end: str
    validate_start: str
    validate_end: str


@dataclass(frozen=True)
class Settings:
    raw: dict

    # --- account ---
    @property
    def capital(self) -> float:
        return float(self.raw["account"]["capital"])

    @property
    def max_leverage(self) -> float:
        return float(self.raw["account"]["max_leverage"])

    def costs(self, name: str) -> Costs:
        return Costs(**self.raw["costs"][name])

    def windows(self, timeframe: str) -> Windows:
        return Windows(**self.raw["windows"][timeframe])

    def section(self, name: str) -> dict:
        return self.raw[name]

    @property
    def research_dir(self) -> Path:
        return Path(os.environ.get("LOX_RESEARCH_DIR", str(ROOT / "research")))


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


@lru_cache(maxsize=1)
def load_settings() -> Settings:
    raw = tomllib.loads(DEFAULTS.read_text())
    override = os.environ.get("LOX_SETTINGS")
    if override:
        raw = _merge(raw, tomllib.loads(Path(override).read_text()))
    if os.environ.get("LOX_GIT_PUSH", "").strip().lower() in {"1", "true", "yes"}:
        raw = _merge(raw, {"git": {"push": True}})
    return Settings(raw)
