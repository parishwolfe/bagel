"""Exact weighted-average cost basis, including long/short reversals."""

from dataclasses import dataclass, field
from decimal import Decimal

ZERO = Decimal(0)
CASH_ACTIVITIES = {
    "CSD",
    "CSW",
    "ACATC",
    "DIV",
    "CGD",
    "DIVFEE",
    "DIVNRA",
    "FEE",
    "INT",
    "INTNRA",
    "INTTW",
    "JNLC",
    "PTC",
    "PTR",
}


@dataclass(frozen=True)
class AccountPosition:
    quantity: Decimal = ZERO
    basis: Decimal = ZERO

    def apply(self, delta: Decimal, price: Decimal) -> tuple["AccountPosition", Decimal]:
        quantity = self.quantity + delta
        if self.quantity * delta >= 0:
            basis = (
                (abs(self.quantity) * self.basis + abs(delta) * price) / abs(quantity) if quantity else ZERO
            )
            realized = ZERO
        else:
            closed = min(abs(self.quantity), abs(delta))
            realized = closed * (price - self.basis) * (1 if self.quantity > 0 else -1)
            basis = (self.basis if self.quantity * quantity > 0 else price) if quantity else ZERO
        return AccountPosition(quantity, basis), realized


@dataclass
class AccountLedger:
    cash: Decimal
    positions: dict[str, AccountPosition] = field(default_factory=dict)
    realized: Decimal = ZERO
    fees: Decimal = ZERO

    def apply(self, activity: dict) -> tuple[str | None, Decimal, Decimal, Decimal, Decimal]:
        kind = activity["activity_type"]
        if kind == "FILL":
            quantity = Decimal(activity["qty"])
            if activity["side"] not in ("buy", "sell") or not quantity.is_finite() or quantity <= 0:
                raise ValueError("Invalid fill quantity or side")
            quantity *= 1 if activity["side"] == "buy" else -1
            price = Decimal(activity["price"])
            if not quantity.is_finite() or not price.is_finite() or not price > 0:
                raise ValueError("Invalid confirmed fill")
            symbol = activity["symbol"]
            position, realized = self.positions.get(symbol, AccountPosition()).apply(quantity, price)
            self.positions[symbol] = position
            cash_delta = -quantity * price
            self.cash += cash_delta
            self.realized += realized
            return symbol, quantity, price, cash_delta, realized
        if kind not in CASH_ACTIVITIES:
            raise ValueError("Unsupported account activity requires reconciliation")
        amount = Decimal(activity["net_amount"])
        if not amount.is_finite():
            raise ValueError("Invalid account cash activity")
        self.cash += amount
        if kind in ("FEE", "DIVFEE", "PTC"):
            self.fees -= amount
        return None, ZERO, ZERO, amount, ZERO

    def dump(self) -> dict:
        return {
            symbol: {"quantity": str(p.quantity), "basis": str(p.basis)}
            for symbol, p in self.positions.items()
            if p.quantity
        }

    @classmethod
    def restore(
        cls, cash: Decimal, positions: dict, realized: Decimal = ZERO, fees: Decimal = ZERO
    ) -> "AccountLedger":
        return cls(
            cash,
            {s: AccountPosition(Decimal(p["quantity"]), Decimal(p["basis"])) for s, p in positions.items()},
            realized,
            fees,
        )
