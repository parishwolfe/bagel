from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from market.data.domain import Bar
from market.portfolio.ledger import ZERO, Fill, Ledger
from market.risk.policy import RiskLimits, order_rejection

BPS = Decimal(10_000)


class FillModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    slippage_bps: Decimal = Field(default=Decimal(2), ge=0, lt=BPS)
    commission_bps: Decimal = Field(default=Decimal(1), ge=0, lt=BPS)
    participation: Decimal = Field(default=Decimal("0.10"), gt=0, le=1)


def simulate_open(
    ledger: Ledger,
    bar: Bar,
    target: int,
    decision_at: datetime,
    known_volume: Decimal,
    costs: FillModel,
    limits: RiskLimits,
    short_eligible: bool,
) -> Fill | str | None:
    if bar.timestamp <= decision_at:
        raise ValueError("Simulated fills require an open strictly after decision availability")
    held = ledger.position(bar.symbol).shares
    if held * target < 0:
        target = 0
    delta = target - held
    if not delta:
        return None
    # A bar's full volume is unknown at its open. Use only volume known at decision time.
    limit = int(known_volume * costs.participation)
    if not limit:
        return "no_known_liquidity"
    shares = min(delta, limit) if delta > 0 else max(delta, -limit)
    slippage = costs.slippage_bps / BPS * (1 if shares > 0 else -1)
    price = bar.open * (1 + slippage)
    fee = abs(shares) * price * costs.commission_bps / BPS if costs.commission_bps else ZERO
    fill = Fill(bar.symbol, bar.timestamp, decision_at, shares, price, fee)
    rejection = order_rejection(ledger, fill, limits, short_eligible)
    if rejection:
        return rejection
    ledger.book(fill)
    return fill
