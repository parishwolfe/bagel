from decimal import Decimal, InvalidOperation
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictBool, model_validator

from market.portfolio.targets import RebalancePolicy
from market.risk.policy import RiskLimits


class RiskPolicy(RiskLimits):
    version: str = "account-risk-v1"
    max_daily_loss_fraction: Decimal = Field(default=Decimal("0.05"), gt=0, le=1)
    max_drawdown_fraction: Decimal = Field(default=Decimal("0.10"), gt=0, le=1)
    account_fresh_seconds: int = Field(default=30, ge=1, le=3600)
    market_fresh_seconds: int = Field(default=120, ge=1, le=86400)
    price_buffer_bps: Decimal = Field(default=Decimal(300), ge=0, le=10000)
    session_policy: Literal["regular", "extended", "queue_next_regular"] = "regular"
    decision_lifetime_seconds: int = Field(default=90, ge=1, le=86400)


class RevisionReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    series_id: int = Field(gt=0)
    bar_start: AwareDatetime
    revision_id: UUID


class ExitLimit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    limit_price: Decimal = Field(gt=0, allow_inf_nan=False)


class ExitStop(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    stop_price: Decimal = Field(gt=0, allow_inf_nan=False)
    limit_price: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)


class StockOrder(BaseModel):
    """All documented Alpaca equity order types; not a paper-only subset."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    symbol: str = Field(pattern=r"^[A-Z][A-Z0-9.\-]{0,19}$")
    side: Literal["buy", "sell"]
    type: Literal["market", "limit", "stop", "stop_limit", "trailing_stop"] = "market"
    time_in_force: Literal["day", "gtc", "opg", "cls", "ioc", "fok"] = "day"
    qty: Decimal | None = Field(default=None, gt=0, decimal_places=9)
    notional: Decimal | None = Field(default=None, gt=0, decimal_places=9)
    limit_price: Decimal | None = Field(default=None, gt=0)
    stop_price: Decimal | None = Field(default=None, gt=0)
    trail_price: Decimal | None = Field(default=None, gt=0)
    trail_percent: Decimal | None = Field(default=None, gt=0, lt=100)
    extended_hours: bool = False
    order_class: Literal["simple", "bracket", "oto", "oco"] = "simple"
    take_profit: ExitLimit | None = None
    stop_loss: ExitStop | None = None

    @model_validator(mode="after")
    def validate_capabilities(self) -> Self:
        if (self.qty is None) == (self.notional is None):
            raise ValueError("Specify exactly one of qty and notional")
        if self.type in ("limit", "stop_limit") and self.limit_price is None:
            raise ValueError("Limit orders require a limit price")
        if self.type in ("stop", "stop_limit") and self.stop_price is None:
            raise ValueError("Stop orders require a stop price")
        if self.type == "trailing_stop" and ((self.trail_price is None) == (self.trail_percent is None)):
            raise ValueError("Trailing stops require exactly one trail parameter")
        if self.type not in ("limit", "stop_limit") and self.limit_price is not None:
            raise ValueError("This order type does not accept limit_price")
        if self.type not in ("stop", "stop_limit") and self.stop_price is not None:
            raise ValueError("This order type does not accept stop_price")
        if self.type != "trailing_stop" and (self.trail_price is not None or self.trail_percent is not None):
            raise ValueError("Only trailing stops accept trail parameters")
        if self.type in ("stop", "stop_limit", "trailing_stop") and self.time_in_force not in ("day", "gtc"):
            raise ValueError("Alpaca stop orders require DAY/GTC")
        if self.extended_hours and (self.type != "limit" or self.time_in_force not in ("day", "gtc")):
            raise ValueError("Alpaca extended hours require DAY/GTC limit orders")
        if self.qty is not None and self.qty != self.qty.to_integral_value() and self.time_in_force != "day":
            raise ValueError("Alpaca fractional equity orders require DAY")
        if self.order_class == "bracket" and (self.take_profit is None or self.stop_loss is None):
            raise ValueError("Bracket orders require both exits")
        if self.order_class == "oto" and ((self.take_profit is None) == (self.stop_loss is None)):
            raise ValueError("OTO orders require exactly one exit")
        if self.order_class == "oco" and (
            self.type != "limit" or self.take_profit is None or self.stop_loss is None
        ):
            raise ValueError("OCO orders require a limit entry and both exit definitions")
        if self.order_class != "simple" and (self.extended_hours or self.time_in_force not in ("day", "gtc")):
            raise ValueError("Alpaca grouped equity orders require regular-hours DAY/GTC")
        if self.order_class == "simple" and (self.take_profit is not None or self.stop_loss is not None):
            raise ValueError("Exit definitions require a grouped order class")
        if self.order_class in ("bracket", "oto") and self.type not in ("market", "limit"):
            raise ValueError("Alpaca grouped entries require market/limit orders")
        if self.order_class != "simple" and self.notional is not None:
            raise ValueError("Alpaca grouped orders require quantity")
        if self.take_profit and self.stop_loss:
            difference = self.take_profit.limit_price - self.stop_loss.stop_price
            higher = (self.side == "buy") == (self.order_class != "oco")
            if (higher and difference <= 0) or (not higher and difference >= 0):
                raise ValueError("Take-profit and stop-loss prices are on the wrong sides")
        for price in (
            self.limit_price,
            self.stop_price,
            self.take_profit.limit_price if self.take_profit else None,
            self.stop_loss.stop_price if self.stop_loss else None,
            self.stop_loss.limit_price if self.stop_loss else None,
        ):
            if price is not None:
                try:
                    rounded = price.quantize(Decimal("0.01") if price >= 1 else Decimal("0.0001"))
                except InvalidOperation:
                    raise ValueError("Price exceeds the supported numeric precision") from None
                if price != rounded:
                    raise ValueError("Price exceeds the documented equity tick precision")
        return self

    def broker_payload(self, client_order_id: str) -> dict:
        return {**self.model_dump(mode="json", exclude_none=True), "client_order_id": client_order_id}


class OrderCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    run_id: UUID
    order: StockOrder
    strategy_id: UUID | None = None
    decision_id: UUID | None = None
    decision_at: AwareDatetime | None = None
    expires_at: AwareDatetime | None = None
    inputs: tuple[RevisionReference, ...] = ()
    signal_id: UUID | None = None

    @model_validator(mode="after")
    def validate_signal(self) -> Self:
        if self.signal_id and (not self.strategy_id or not self.decision_at or self.inputs):
            raise ValueError("Signal-backed orders require strategy/decision time and no copied inputs")
        return self


class TargetCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    run_id: UUID
    strategy_id: UUID
    decision_id: UUID
    decision_at: AwareDatetime
    expires_at: AwareDatetime
    symbol: str = Field(pattern=r"^[A-Z][A-Z0-9.\-]{0,19}$")
    weight: Decimal = Field(ge=-1, le=1)
    price: Decimal = Field(gt=0)
    inputs: tuple[RevisionReference, ...] = ()
    signal_id: UUID | None = None
    activation_at: AwareDatetime | None = None
    rebalance_policy: RebalancePolicy = "on_weight_change"


class ArmCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    run_id: UUID
    expires_at: AwareDatetime
    policy: RiskPolicy = Field(default_factory=RiskPolicy)


class CancelCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    broker_order_id: UUID


class AccountView(BaseModel):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)
    id: UUID
    status: str
    cash: Decimal
    equity: Decimal
    buying_power: Decimal
    trading_blocked: StrictBool
    shorting_enabled: StrictBool = False


class BrokerAssetView(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: UUID
    asset_class: str = Field(alias="class")
    status: str
    tradable: StrictBool
    fractionable: StrictBool = False
    shortable: StrictBool = False
    easy_to_borrow: StrictBool = False


class BrokerClockView(BaseModel):
    model_config = ConfigDict(extra="ignore")
    timestamp: AwareDatetime
    is_open: StrictBool


class BrokerOrderView(BaseModel):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)
    id: UUID
    asset_class: Literal["us_equity"] = "us_equity"
    client_order_id: str = Field(min_length=1, max_length=128)
    symbol: str = Field(min_length=1, max_length=80)
    side: Literal["buy", "sell"]
    status: str = Field(min_length=1, max_length=80)
    qty: Decimal | None = Field(default=None, gt=0)
    filled_qty: Decimal = Field(ge=0)
    created_at: AwareDatetime
    updated_at: AwareDatetime | None = None


class BrokerPositionView(BaseModel):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)
    symbol: str = Field(min_length=1, max_length=80)
    asset_class: Literal["us_equity"] = "us_equity"
    qty: Decimal
    current_price: Decimal = Field(gt=0)
