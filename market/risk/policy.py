from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from market.portfolio.ledger import ZERO, Fill, Ledger


class RiskLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_position_fraction: Decimal = Field(default=Decimal("0.25"), gt=0, le=1)
    max_gross_fraction: Decimal = Field(default=Decimal(1), gt=0, le=4)
    max_net_fraction: Decimal = Field(default=Decimal(1), gt=0, le=4)
    max_short_fraction: Decimal = Field(default=Decimal("0.50"), ge=0, le=2)
    initial_short_margin: Decimal = Field(default=Decimal("0.50"), gt=0, le=2)
    maintenance_short_margin: Decimal = Field(default=Decimal("0.30"), gt=0, le=2)
    min_margin_buffer: Decimal = Field(default=ZERO, ge=0)


def order_rejection(
    ledger: Ledger, fill: Fill, limits: RiskLimits, short_eligible: bool
) -> str | None:
    previous = ledger.position(fill.instrument)
    after, _ = previous.apply(fill.shares, fill.price)
    if abs(after.shares) < abs(previous.shares):
        return None
    if after.shares < 0 and not short_eligible:
        return "borrow_unavailable"
    positions = {**ledger.positions, fill.instrument: after}
    marks = {**ledger.marks, fill.instrument: max(ledger.marks[fill.instrument], fill.price)}
    exposures = [position.shares * marks[i] for i, position in positions.items()]
    cash = ledger.cash - fill.shares * fill.price - fill.fee
    equity = cash + sum((p.shares * ledger.marks[i] for i, p in positions.items()), ZERO)
    short = -sum((value for value in exposures if value < 0), ZERO)
    if equity <= 0:
        return "no_equity"
    if cash < sum((p.restricted_proceeds for p in positions.values()), ZERO):
        return "insufficient_unrestricted_cash"
    if abs(after.shares * marks[fill.instrument]) > equity * limits.max_position_fraction:
        return "position_limit"
    if sum(map(abs, exposures), ZERO) > equity * limits.max_gross_fraction:
        return "gross_limit"
    if abs(sum(exposures, ZERO)) > equity * limits.max_net_fraction:
        return "net_limit"
    if short > equity * limits.max_short_fraction:
        return "short_limit"
    required = short * max(limits.initial_short_margin, limits.maintenance_short_margin)
    if short and equity - required < limits.min_margin_buffer:
        return "short_margin"
    return None
