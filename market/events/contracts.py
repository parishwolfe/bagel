"""Versioned, immutable event contracts, shared by adapters and replay."""

from datetime import timedelta
from decimal import Decimal
from typing import Literal, Self
from uuid import UUID, uuid5

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from market.data.domain import Bar, Observation
from market.research.artifacts import digest_json

EVENT_NAMESPACE = UUID("f91bc7aa-45ad-48aa-b040-e74b0957e608")


class BarPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    interval_seconds: Literal[60] = 60
    start: AwareDatetime
    end: AwareDatetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    vwap: Decimal | None = None
    trade_count: int | None = None
    session: Literal["pre", "regular", "post"]
    adjustment: Literal["raw"] = "raw"
    currency: Literal["USD"] = "USD"

    @model_validator(mode="after")
    def validate_bar(self) -> Self:
        if self.end - self.start != timedelta(seconds=self.interval_seconds):
            raise ValueError("Bar interval does not match boundaries")
        self.bar("validation")
        return self

    def bar(self, symbol: str) -> Bar:
        return Bar(
            symbol,
            self.start,
            self.open,
            self.high,
            self.low,
            self.close,
            self.volume,
            self.vwap,
            self.trade_count,
        )


class BarEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    event_type: Literal["market.bar.observed"] = "market.bar.observed"
    event_id: UUID
    revision_id: UUID
    source: str = Field(min_length=1, max_length=80)
    provider: str = Field(min_length=1, max_length=80)
    feed: str = Field(min_length=1, max_length=80)
    instrument_id: UUID
    provider_symbol: str = Field(min_length=1, max_length=80)
    series_id: int = Field(gt=0)
    source_event_time: AwareDatetime
    received_at: AwareDatetime
    observed_at: AwareDatetime
    available_at: AwareDatetime
    origin: Literal["live", "live_updated", "historical"]
    payload_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    availability_policy: Literal["observed_receipt", "legacy_snapshot"] = "observed_receipt"
    quality: tuple[str, ...] = ()
    payload: BarPayload

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        if self.available_at < max(self.received_at, self.payload.end):
            raise ValueError("A completed bar cannot be available before receipt or completion")
        if self.payload_sha256 != digest_json(self.payload.model_dump(mode="json")):
            raise ValueError("Payload checksum mismatch")
        expected = uuid5(EVENT_NAMESPACE, f"{self.series_id}:{self.origin}:{self.payload_sha256}")
        if self.event_id != expected or self.revision_id != expected:
            raise ValueError("Event identity mismatch")
        return self


class MarketEvent(BaseModel):
    """Wire event independent of database keys; consumers resolve their own mappings."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[2] = 2
    event_type: Literal["market.bar.observed"] = "market.bar.observed"
    event_id: UUID
    provider: str = Field(min_length=1, max_length=80)
    feed: str = Field(min_length=1, max_length=80)
    provider_instrument_id: str = Field(min_length=1, max_length=100)
    provider_symbol: str = Field(pattern=r"^[A-Z][A-Z0-9.\-]{0,19}$")
    received_at: AwareDatetime
    observed_at: AwareDatetime
    available_at: AwareDatetime
    origin: Literal["live", "live_updated", "historical"]
    payload_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    payload: BarPayload

    @model_validator(mode="after")
    def valid_identity(self) -> Self:
        if self.available_at < max(self.received_at, self.payload.end):
            raise ValueError("Market event precedes receipt or bar completion")
        if digest_json(self.payload.model_dump(mode="json")) != self.payload_sha256:
            raise ValueError("Market event checksum mismatch")
        expected = uuid5(
            EVENT_NAMESPACE,
            f"v2:{self.provider}:{self.feed}:{self.provider_instrument_id}:{self.origin}:{self.payload_sha256}",
        )
        if expected != self.event_id:
            raise ValueError("Market event identity mismatch")
        return self


def market_event(
    observation: Observation, *, provider: str, feed: str, provider_instrument_id: str
) -> MarketEvent:
    bar = observation.bar
    payload = BarPayload.model_validate(
        dict(
            start=bar.timestamp,
            end=bar.timestamp + timedelta(minutes=1),
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
            vwap=bar.vwap,
            trade_count=bar.trade_count,
            session=observation.session,
        )
    )
    checksum = digest_json(payload.model_dump(mode="json"))
    identity = uuid5(
        EVENT_NAMESPACE, f"v2:{provider}:{feed}:{provider_instrument_id}:{observation.origin.value}:{checksum}"
    )
    return MarketEvent(
        event_id=identity,
        provider=provider,
        feed=feed,
        provider_instrument_id=provider_instrument_id,
        provider_symbol=bar.symbol,
        received_at=observation.received_at,
        observed_at=observation.observed_at,
        available_at=max(payload.end, observation.received_at),
        origin=observation.origin.value,
        payload_sha256=checksum,
        payload=payload,
    )


def event_from_observation(
    observation: Observation, *, series_id: int, instrument_id: UUID, source: str, feed: str
) -> BarEvent:
    bar = observation.bar
    payload = BarPayload.model_validate(
        {
            "start": bar.timestamp,
            "end": bar.timestamp + timedelta(minutes=1),
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
            "vwap": bar.vwap,
            "trade_count": bar.trade_count,
            "session": observation.session,
        }
    )
    checksum = digest_json(payload.model_dump(mode="json"))
    identity = uuid5(EVENT_NAMESPACE, f"{series_id}:{observation.origin.value}:{checksum}")
    return BarEvent(
        event_id=identity,
        revision_id=identity,
        source=source,
        provider=source,
        feed=feed,
        instrument_id=instrument_id,
        provider_symbol=bar.symbol,
        series_id=series_id,
        source_event_time=bar.timestamp,
        received_at=observation.received_at,
        observed_at=observation.observed_at,
        available_at=max(payload.end, observation.received_at),
        origin=observation.origin.value,
        payload_sha256=checksum,
        payload=payload,
    )
