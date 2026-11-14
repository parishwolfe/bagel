"""Immutable strategy configuration, selected universe, and explicit controls."""

import hashlib
from decimal import Decimal
from functools import cache
from pathlib import Path
from typing import Literal, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import Engine, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert

from market import models as m
from market.database import database_session, row_dict
from market.events.commands import append_command
from market.research.artifacts import digest_json
from market.research.queue import engine_id as research_engine_id
from market.strategies.core import StrategySpec


@cache
def strategy_engine_id() -> str:
    directory = Path(__file__).parent
    return digest_json(
        {
            "shared_engine": research_engine_id(),
            "actor_files": {
                name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
                for name in ("service.py", "worker.py")
            },
        }
    )


class StrategyRunSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    name: str = Field(min_length=1, max_length=100)
    strategy: StrategySpec = Field(default_factory=StrategySpec)
    source: str = Field(default="alpaca", pattern=r"^[a-z][a-z0-9_-]{0,79}$")
    feed: str = Field(default="iex", min_length=1, max_length=80)
    symbols: tuple[str, ...] = ()
    mode: Literal["signals", "paper"] = "signals"
    account_id: UUID | None = None
    trading_run_id: UUID | None = None
    capital_fraction: Decimal = Field(default=Decimal(1), gt=0, le=1)
    frame_delay_seconds: int = Field(default=5, ge=1, le=300)
    missing_policy: Literal["available", "require_all"] = "require_all"
    min_coverage_fraction: Decimal = Field(default=Decimal(1), gt=0, le=1)
    decision_lifetime_seconds: int = Field(default=90, ge=1, le=3600)
    max_buffered_frames: int = Field(default=120, ge=1, le=1440)
    state_value_budget: int = Field(default=2_000_000, ge=1)

    @model_validator(mode="after")
    def validate_run(self) -> Self:
        if self.mode == "paper" and (self.account_id is None or self.trading_run_id is None):
            raise ValueError("Paper strategies require an account and an explicitly armed trading run")
        if self.mode == "paper" and self.source != "alpaca":
            raise ValueError("This execution adapter requires Alpaca decision and risk feeds to match")
        if self.mode == "paper" and self.feed not in ("iex", "sip"):
            raise ValueError("Paper equity strategies require an entitled Alpaca equity feed")
        if len(set(self.symbols)) != len(self.symbols) or any(not s or len(s) > 80 for s in self.symbols):
            raise ValueError("Strategy symbols must be unique nonempty provider symbols")
        return self


class Strategies:
    def __init__(self, engine: Engine):
        self.engine = engine

    def create(self, spec: StrategyRunSpec) -> UUID:
        payload = spec.model_dump(mode="json")
        request_hash = digest_json((strategy_engine_id(), payload))
        with database_session(self.engine) as conn:
            if spec.mode == "paper":
                account = conn.get(m.TradingAccount, spec.account_id)
                if not account or account.snapshot.get("feed") != spec.feed:
                    raise ValueError("Paper strategy account and feed must match a verified local account")
            existing = conn.scalar(select(m.StrategyRun.id).where(m.StrategyRun.request_hash == request_hash))
            if existing:
                return existing
            statement = (
                select(m.Series.id.label("series_id"), m.Series.instrument_id, m.InstrumentMapping.symbol)
                .join(m.Source, m.Source.id == m.Series.source_id)
                .join(m.IngestedUniverse, m.IngestedUniverse.series_id == m.Series.id)
                .join(
                    m.InstrumentMapping,
                    (m.InstrumentMapping.instrument_id == m.Series.instrument_id)
                    & (m.InstrumentMapping.source_id == m.Source.id),
                )
                .where(
                    m.Source.code == spec.source,
                    m.Series.feed == spec.feed,
                    m.Series.adjustment == "raw",
                    m.Series.interval_seconds == 60,
                    m.Series.currency == "USD",
                    m.InstrumentMapping.valid_to.is_(None),
                )
            )
            if spec.symbols:
                statement = statement.where(m.InstrumentMapping.symbol.in_(spec.symbols))
            members = conn.execute(statement.order_by(m.Series.id)).mappings().all()
            if not members or (spec.symbols and {m["symbol"] for m in members} != set(spec.symbols)):
                raise ValueError(
                    "Every selected symbol needs ingested raw one-minute data in the selected feed"
                )
            if len({m["instrument_id"] for m in members}) != len(members):
                raise ValueError("Selected series have ambiguous current provider mappings")
            values_per_member = (
                spec.strategy.slow + 1
                if spec.strategy.name == "cross_section_momentum"
                else spec.strategy.fast + spec.strategy.slow
                if spec.strategy.name == "sma_cross"
                else 1
            )
            if len(members) * values_per_member > spec.state_value_budget:
                raise ValueError("Selected strategy exceeds its configured state-value resource budget")
            identity = uuid4()
            row = conn.scalar(
                insert(m.StrategyRun)
                .values(id=identity, request_hash=request_hash, spec=payload, engine_id=strategy_engine_id())
                .on_conflict_do_nothing(index_elements=[m.StrategyRun.request_hash])
                .returning(m.StrategyRun.id)
            )
            if row is None:
                existing = conn.scalar(
                    select(m.StrategyRun.id).where(m.StrategyRun.request_hash == request_hash)
                )
                assert existing
                return existing
            conn.execute(
                insert(m.StrategyMember), [dict(run_id=identity, **dict(member)) for member in members]
            )
            append_command(
                conn,
                identity,
                "command.strategy.created",
                str(identity),
                {"strategy_id": str(identity), "members": len(members)},
            )
        return identity

    def control(self, identity: UUID, running: bool) -> dict:
        with database_session(self.engine) as conn:
            row = conn.scalar(
                select(m.StrategyRun).where(m.StrategyRun.id == identity).with_for_update(key_share=True)
            )
            if row is None:
                raise KeyError("Strategy run not found")
            if running and row.engine_id != strategy_engine_id():
                raise ValueError("Strategy run belongs to a different engine; create a new versioned run")
            if running and row.state != "running":
                conn.execute(
                    update(m.StrategyRun)
                    .where(m.StrategyRun.id == identity)
                    .values(
                        state="running",
                        reason="warming",
                        owner_token=None,
                        lease_until=None,
                        activated_at=func.clock_timestamp(),
                        last_frame=None,
                        checkpoint={},
                        warmed=False,
                        cursor=0,
                        updated_at=func.clock_timestamp(),
                    )
                )
                conn.execute(delete(m.StrategyFrameBar).where(m.StrategyFrameBar.run_id == identity))
            elif not running:
                conn.execute(
                    update(m.StrategyRun)
                    .where(m.StrategyRun.id == identity)
                    .values(
                        state="paused",
                        reason="operator_paused",
                        owner_token=None,
                        lease_until=None,
                        updated_at=func.clock_timestamp(),
                    )
                )
                conn.execute(
                    update(m.OrderIntent)
                    .where(
                        m.OrderIntent.strategy_id == identity,
                        m.OrderIntent.state == "admitted",
                        m.OrderIntent.dispatch_count == 0,
                    )
                    .values(state="expired", reserved_notional=0, reason="strategy_paused")
                )
            append_command(
                conn,
                uuid4(),
                "command.strategy." + ("start" if running else "pause"),
                str(identity),
                {"strategy_id": str(identity)},
            )
        return self.get(identity)

    def runs(self, offset: int = 0, limit: int = 50) -> list[dict]:
        with database_session(self.engine) as conn:
            count = (
                select(func.count())
                .select_from(m.StrategyMember)
                .where(m.StrategyMember.run_id == m.StrategyRun.id)
                .scalar_subquery()
            )
            rows = [
                {**row_dict(run), "members": members}
                for run, members in conn.execute(
                    select(m.StrategyRun, count)
                    .order_by(m.StrategyRun.created_at.desc(), m.StrategyRun.id)
                    .offset(offset)
                    .limit(limit)
                )
            ]
        return [{**row, "engine_compatible": row["engine_id"] == strategy_engine_id()} for row in rows]

    def get(self, identity: UUID) -> dict:
        with database_session(self.engine) as conn:
            instance = conn.get(m.StrategyRun, identity)
            if instance is None:
                raise KeyError("Strategy run not found")
            row = row_dict(instance)
            row["members"] = {
                "count": conn.scalar(
                    select(func.count())
                    .select_from(m.StrategyMember)
                    .where(m.StrategyMember.run_id == identity)
                )
            }
            return {**row, "engine_compatible": row["engine_id"] == strategy_engine_id()}

    def signals(self, identity: UUID, offset: int = 0, limit: int = 50) -> list[dict]:
        with database_session(self.engine) as conn:
            return [
                row_dict(r)
                for r in conn.scalars(
                    select(m.StrategySignal)
                    .where(m.StrategySignal.run_id == identity)
                    .order_by(m.StrategySignal.bar_start.desc(), m.StrategySignal.id)
                    .offset(offset)
                    .limit(limit)
                )
            ]
