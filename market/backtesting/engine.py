from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from heapq import heapify, heappop, heappush
from itertools import count, groupby
from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from market.data.domain import MINUTE, NEW_YORK, Bar
from market.execution.simulator import FillModel, simulate_open
from market.portfolio.ledger import ZERO, Fill, Ledger
from market.portfolio.targets import target_quantity
from market.risk.policy import RiskLimits
from market.strategies.core import StrategySpec
from market.strategies.runtime import StrategyRuntime

ENGINE_VERSION = "equity-minute-v2"


class BacktestSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    strategy: StrategySpec = Field(default_factory=StrategySpec)
    initial_cash: Decimal = Field(default=Decimal(100_000), gt=0, le=Decimal("1e10"))
    start: AwareDatetime | None = None
    end: AwareDatetime | None = None
    split: Literal["exploratory", "train", "validation", "holdout"] = "exploratory"
    instruments: tuple[str, ...] = ()
    data_delay_seconds: int = Field(default=5, ge=0, le=900)
    signal_lifetime_seconds: int = Field(default=600, ge=60, le=3600)
    missing_policy: Literal["available", "require_all"] = "available"
    min_coverage_fraction: Decimal = Field(default=Decimal(1), gt=0, le=1)
    fill_model: FillModel = Field(default_factory=FillModel)
    risk: RiskLimits = Field(default_factory=RiskLimits)
    borrow_instruments: tuple[str, ...] = ()
    borrow_annual_bps: Decimal = Field(default=Decimal(500), ge=0, le=Decimal(100_000))

    @field_validator("instruments", "borrow_instruments")
    @classmethod
    def canonical_instruments(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not v or len(v) > 100 for v in values):
            raise ValueError("Invalid instrument identifier")
        return tuple(sorted(set(values)))

    @model_validator(mode="after")
    def check_window(self) -> Self:
        if self.start and self.end and self.start >= self.end:
            raise ValueError("start must precede end")
        if self.split != "exploratory" and (self.start is None or self.end is None):
            raise ValueError("Labeled splits require explicit start and end")
        return self


class CashOrSplitEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    instrument: str = Field(min_length=1, max_length=100)
    effective_at: AwareDatetime
    kind: Literal["split", "cash_distribution"]
    value: Decimal = Field(gt=0)


@dataclass(frozen=True)
class Intent:
    target: int
    available_at: datetime
    known_volume: Decimal


def run_backtest(
    bars: Iterable[Bar],
    spec: BacktestSpec,
    actions: tuple[CashOrSplitEvent, ...] = (),
    checkpoint: Callable[[], None] | None = None,
    fill_sink: Callable[[Fill], None] | None = None,
) -> dict:
    ledger = Ledger(spec.initial_cash)
    fills: list[Fill] = []
    fill_count = 0
    rejections: Counter[str] = Counter()
    runtime = StrategyRuntime(spec.strategy)
    queued: list[tuple[datetime, int, str, Intent]] = []
    order = count()
    pending: dict[str, Intent] = {}
    targets: dict[str, Decimal] = {}
    quantities: dict[str, Decimal] = {}
    daily: dict[str, Decimal] = {}
    closing_marks: dict[str, Decimal] = {}
    peak = spec.initial_cash
    max_drawdown = ZERO
    delay = timedelta(seconds=spec.data_delay_seconds)
    lifetime = timedelta(seconds=spec.signal_lifetime_seconds)
    instruments = set(spec.instruments)
    borrow = set(spec.borrow_instruments)
    action_queue = iter(sorted(actions, key=lambda a: a.effective_at))
    action = next(action_queue, None)
    previous: datetime | None = None
    count_bars = 0
    for timestamp, frame in groupby(bars, key=lambda b: b.timestamp):
        if checkpoint:
            checkpoint()
        if spec.end and timestamp + MINUTE > spec.end:
            break
        trading = spec.start is None or timestamp >= spec.start
        batch = sorted(
            (b for b in frame if not instruments or b.symbol in instruments), key=lambda b: b.symbol
        )
        if not batch:
            continue
        if trading and previous:
            ledger.charge_borrow(int((timestamp - previous).total_seconds()), spec.borrow_annual_bps)
        while action and action.effective_at <= timestamp:
            if action.kind == "split":
                ledger.split(action.instrument, action.value)
                if action.instrument in closing_marks:
                    closing_marks[action.instrument] /= action.value
                runtime.reset(action.instrument)
                targets.pop(action.instrument, None)
                quantities.pop(action.instrument, None)
                pending.pop(action.instrument, None)
                # Signals computed on pre-split prices cannot size a post-split order.
                queued = [entry for entry in queued if entry[2] != action.instrument]
                heapify(queued)
            else:
                ledger.distribute(action.instrument, action.value)
            action = next(action_queue, None)
        if trading:
            count_bars += len(batch)
            ledger.marks.update((bar.symbol, bar.open) for bar in batch)
            while queued and queued[0][0] < timestamp:
                _, _, instrument, intent = heappop(queued)
                pending[instrument] = intent
            for bar in batch:
                intent = pending.get(bar.symbol)
                if intent is None:
                    continue
                if timestamp >= intent.available_at + lifetime:
                    pending.pop(bar.symbol)
                    rejections["expired_signal"] += 1
                    continue
                outcome = simulate_open(
                    ledger,
                    bar,
                    intent.target,
                    intent.available_at,
                    intent.known_volume,
                    spec.fill_model,
                    spec.risk,
                    bar.symbol in borrow,
                )
                if isinstance(outcome, Fill):
                    fill_count += 1
                    if fill_sink is not None:
                        fill_sink(outcome)
                    else:
                        fills.append(outcome)
                elif isinstance(outcome, str):
                    rejections[outcome] += 1
                if ledger.position(bar.symbol).shares == intent.target:
                    pending.pop(bar.symbol)
            previous = timestamp
        closing_marks.update((bar.symbol, bar.close) for bar in batch)
        equity = ledger.equity_at(closing_marks)
        if trading:
            peak = max(peak, equity)
            max_drawdown = max(max_drawdown, (peak - equity) / peak)
            daily[timestamp.astimezone(NEW_YORK).date().isoformat()] = equity
        coverage = Decimal(len(batch)) / Decimal(len(instruments)) if instruments else Decimal(1)
        complete = not instruments or len(batch) == len(instruments)
        eligible = (complete or spec.missing_policy == "available") and coverage >= spec.min_coverage_fraction
        weights = runtime.on_frame(batch) if eligible else {}
        for bar in batch:
            weight = weights.get(bar.symbol)
            if trading and weight is not None:
                quantity = target_quantity(
                    weight,
                    equity,
                    bar.close,
                    targets.get(bar.symbol),
                    quantities.get(bar.symbol),
                    spec.strategy.rebalance_policy,
                )
                changed = (
                    spec.strategy.rebalance_policy == "every_frame"
                    or targets.get(bar.symbol) != weight
                    or quantities.get(bar.symbol) != quantity
                )
                if not changed:
                    continue
                targets[bar.symbol] = weight
                quantities[bar.symbol] = Decimal(quantity)
                available = timestamp + MINUTE + delay
                # Fix whole-share size using the signal bar, before any later open is observed.
                intent = Intent(quantity, available, bar.volume)
                heappush(queued, (available, next(order), bar.symbol, intent))
    if not count_bars:
        raise ValueError("No bars match the requested window and instruments")
    if checkpoint:
        checkpoint()
    final_equity = ledger.equity_at(closing_marks)
    return {
        "summary": {
            "engine_version": ENGINE_VERSION,
            "initial_equity": spec.initial_cash,
            "final_equity": final_equity,
            "return_fraction": final_equity / spec.initial_cash - 1,
            "max_drawdown_fraction": max_drawdown,
            "realized_pnl_before_costs": ledger.realized_pnl,
            "fees": ledger.fees,
            "borrow_costs": ledger.financing,
            "cash_distributions": ledger.distributions,
            "fills": fill_count,
            "bars": count_bars,
            "rejections": dict(rejections),
            "borrow_is_scenario": bool(borrow),
            "open_positions": {key: value.shares for key, value in ledger.positions.items()},
        },
        "daily_equity": daily,
        "fills": [asdict(fill) for fill in fills],
    }
