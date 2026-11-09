from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

ZERO = Decimal(0)


@dataclass(frozen=True)
class Position:
    shares: int = 0
    basis: Decimal = ZERO

    @property
    def restricted_proceeds(self) -> Decimal:
        return -min(0, self.shares) * self.basis

    def apply(self, delta: int, price: Decimal) -> tuple["Position", Decimal]:
        shares = self.shares + delta
        if self.shares * shares < 0:
            raise ValueError("Position reversals must close before opening the opposite side")
        if not shares:
            basis = ZERO
        elif self.shares * delta >= 0:
            basis = (abs(self.shares) * self.basis + abs(delta) * price) / abs(shares)
        else:
            basis = self.basis
        realized = -delta * (price - self.basis) if self.shares * delta < 0 else ZERO
        return Position(shares, basis), realized


@dataclass(frozen=True)
class Fill:
    instrument: str
    timestamp: datetime
    decision_at: datetime
    shares: int
    price: Decimal
    fee: Decimal


@dataclass
class Ledger:
    cash: Decimal
    positions: dict[str, Position] = field(default_factory=dict)
    marks: dict[str, Decimal] = field(default_factory=dict)
    realized_pnl: Decimal = ZERO
    fees: Decimal = ZERO
    financing: Decimal = ZERO
    distributions: Decimal = ZERO

    def position(self, instrument: str) -> Position:
        return self.positions.get(instrument, Position())

    @property
    def equity(self) -> Decimal:
        return self.equity_at(self.marks)

    def equity_at(self, marks: dict[str, Decimal]) -> Decimal:
        return self.cash + sum((p.shares * marks[i] for i, p in self.positions.items()), ZERO)

    @property
    def short_notional(self) -> Decimal:
        return sum(
            (-p.shares * self.marks[i] for i, p in self.positions.items() if p.shares < 0), ZERO
        )

    def book(self, fill: Fill) -> None:
        position, realized = self.position(fill.instrument).apply(fill.shares, fill.price)
        self.cash -= fill.shares * fill.price + fill.fee
        self.realized_pnl += realized
        self.fees += fill.fee
        if position.shares:
            self.positions[fill.instrument] = position
        else:
            self.positions.pop(fill.instrument, None)

    def charge_borrow(self, seconds: int, annual_bps: Decimal) -> None:
        cost = self.short_notional * annual_bps * seconds / Decimal(315_360_000_000)
        self.cash -= cost
        self.financing += cost

    def distribute(self, instrument: str, per_share: Decimal) -> None:
        amount = self.position(instrument).shares * per_share
        self.cash += amount
        self.distributions += amount

    def split(self, instrument: str, ratio: Decimal) -> None:
        position = self.position(instrument)
        shares = position.shares * ratio
        if shares != int(shares):
            raise ValueError("Split produces fractional shares. Supply a cash-in-lieu model first.")
        if shares:
            self.positions[instrument] = Position(int(shares), position.basis / ratio)
        if instrument in self.marks:
            self.marks[instrument] /= ratio
