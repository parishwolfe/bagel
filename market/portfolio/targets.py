"""Shared whole-share target sizing, independent of broker/fill implementation."""

from decimal import Decimal
from typing import Literal

RebalancePolicy = Literal["on_weight_change", "every_frame"]


def target_quantity(
    weight: Decimal,
    equity: Decimal,
    price: Decimal,
    previous_weight: Decimal | None,
    previous_quantity: Decimal | None,
    policy: RebalancePolicy,
) -> int:
    if policy not in ("on_weight_change", "every_frame"):
        raise ValueError("Unsupported target rebalance policy")
    if not weight.is_finite() or not equity.is_finite() or not price.is_finite() or price <= 0:
        raise ValueError("Invalid target sizing inputs")
    if policy == "on_weight_change" and previous_weight == weight and previous_quantity is not None:
        if not previous_quantity.is_finite() or previous_quantity != previous_quantity.to_integral_value():
            raise ValueError("Invalid whole-share target checkpoint")
        return int(previous_quantity)
    return int(weight * max(Decimal(0), equity) / price)
