"""Provider-free, checkpointable strategy registry shared by replay and paper.

A frame contains only completed bars for one timestamp. Missing members are never
carried forward; cross-sectional ranks use only contemporaneous, warmed members.
"""

from collections import deque
from collections.abc import Sequence
from decimal import Decimal

from market.data.domain import Bar
from market.strategies.core import Strategy, StrategySpec, create_strategy

REGISTRY = {
    "sma_cross": {"version": 1, "scope": "instrument"},
    "buy_and_hold": {"version": 1, "scope": "instrument"},
    "cash": {"version": 1, "scope": "instrument"},
    "cross_section_momentum": {"version": 1, "scope": "frame"},
}
RUNTIME_VERSION = 1


class StrategyRuntime:
    def __init__(self, spec: StrategySpec, state: dict | None = None):
        self.spec = spec
        self.strategies: dict[str, Strategy] = {}
        self.history: dict[str, deque[Decimal]] = {}
        if state:
            if state.get("version") != RUNTIME_VERSION or state.get("spec") != spec.model_dump(mode="json"):
                raise ValueError("Strategy checkpoint version or configuration mismatch")
            for symbol, values in state.get("history", {}).items():
                self.history[symbol] = deque((Decimal(v) for v in values), maxlen=spec.slow + 1)
            for symbol, checkpoint in state.get("instruments", {}).items():
                strategy = create_strategy(spec)
                strategy.restore(checkpoint)
                self.strategies[symbol] = strategy

    def reset(self, symbol: str) -> None:
        self.strategies.pop(symbol, None)
        self.history.pop(symbol, None)

    def on_frame(self, bars: Sequence[Bar]) -> dict[str, Decimal]:
        if len({bar.timestamp for bar in bars}) > 1 or len({bar.symbol for bar in bars}) != len(bars):
            raise ValueError("Strategy frames require unique instruments at one timestamp")
        targets: dict[str, Decimal] = {}
        if self.spec.name != "cross_section_momentum":
            for bar in sorted(bars, key=lambda value: value.symbol):
                strategy = self.strategies.get(bar.symbol)
                if strategy is None:
                    strategy = self.strategies[bar.symbol] = create_strategy(self.spec)
                target = strategy.on_close(bar.close)
                if target is not None:
                    targets[bar.symbol] = target
            return targets
        scores = []
        for bar in bars:
            history = self.history.setdefault(bar.symbol, deque(maxlen=self.spec.slow + 1))
            history.append(bar.close)
            if len(history) == history.maxlen:
                scores.append((bar.close / history[0] - 1, bar.symbol))
        required = self.spec.rank_count * (2 if self.spec.short_below else 1)
        if len(scores) < required:
            return {}
        ranked = sorted(scores, key=lambda entry: (-entry[0], entry[1]))
        targets = {symbol: Decimal(0) for _, symbol in ranked}
        weight = self.spec.target_weight / self.spec.rank_count
        targets.update((symbol, weight) for _, symbol in ranked[: self.spec.rank_count])
        if self.spec.short_below:
            targets.update((symbol, -weight) for _, symbol in ranked[-self.spec.rank_count :])
        return targets

    def checkpoint(self) -> dict:
        return {
            "version": RUNTIME_VERSION,
            "spec": self.spec.model_dump(mode="json"),
            "instruments": {symbol: strategy.checkpoint() for symbol, strategy in self.strategies.items()},
            "history": {symbol: [str(v) for v in values] for symbol, values in self.history.items()},
        }
