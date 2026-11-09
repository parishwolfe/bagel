from collections import deque
from decimal import Decimal
from typing import Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrategySpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: Literal["sma_cross", "buy_and_hold", "cash"] = "sma_cross"
    fast: int = Field(default=5, ge=1, le=10000)
    slow: int = Field(default=20, ge=2, le=10000)
    target_weight: Decimal = Field(default=Decimal("0.10"), gt=0, le=1)
    short_below: bool = False

    @model_validator(mode="after")
    def check_windows(self) -> Self:
        if self.name == "sma_cross" and self.fast >= self.slow:
            raise ValueError("fast must be shorter than slow")
        return self


class Strategy(Protocol):
    def on_close(self, close: Decimal) -> Decimal | None: ...


class MovingAverage:
    def __init__(self, length: int):
        self.values: deque[Decimal] = deque(maxlen=length)
        self.total = Decimal(0)

    def update(self, value: Decimal) -> Decimal | None:
        if len(self.values) == self.values.maxlen:
            self.total -= self.values[0]
        self.values.append(value)
        self.total += value
        return self.total / len(self.values) if len(self.values) == self.values.maxlen else None


class MovingAverageCross:
    def __init__(self, spec: StrategySpec):
        self.spec = spec
        self.fast = MovingAverage(spec.fast)
        self.slow = MovingAverage(spec.slow)

    def on_close(self, close: Decimal) -> Decimal | None:
        fast = self.fast.update(close)
        slow = self.slow.update(close)
        if fast is None or slow is None:
            return None
        if fast > slow:
            return self.spec.target_weight
        if fast < slow and self.spec.short_below:
            return -self.spec.target_weight
        return Decimal(0)


class ConstantTarget:
    def __init__(self, target: Decimal):
        self.target = target

    def on_close(self, close: Decimal) -> Decimal:
        return self.target


def create_strategy(spec: StrategySpec) -> Strategy:
    if spec.name == "sma_cross":
        return MovingAverageCross(spec)
    return ConstantTarget(spec.target_weight if spec.name == "buy_and_hold" else Decimal(0))
