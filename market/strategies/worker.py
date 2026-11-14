"""Redis-fed strategy actors; ORM is used only by control/bootstrap and persistence."""

import json
import logging
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import groupby
from typing import Any, TypedDict, cast
from uuid import UUID, uuid5

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from pydantic_core import to_jsonable_python
from sqlalchemy import Engine, and_, func, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from market import models as m
from market.database import database_session, row_dict
from market.events.bus import MarketBus
from market.events.contracts import BarPayload, MarketEvent
from market.research.artifacts import digest_json
from market.strategies.runtime import StrategyRuntime
from market.strategies.service import StrategyRunSpec, strategy_engine_id

log = logging.getLogger(__name__)


class ActorState(TypedDict):
    checkpoint: dict
    buffer: dict[str, dict[str, dict]]
    last_frame: str | None
    frames: int
    skipped_frames: int
    late_revisions: int
    events_processed: int
    warmed: bool


class SignalInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    series_id: int = Field(gt=0)
    instrument_id: UUID
    symbol: str
    bar_start: AwareDatetime
    revision_id: UUID
    price: Decimal = Field(gt=0, allow_inf_nan=False)


class StrategyEmission(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    run_id: UUID
    activation_at: AwareDatetime
    engine_id: str
    checkpoint: dict
    frames: int = Field(ge=0)
    skipped_frames: int = Field(ge=0)
    late_revisions: int = Field(ge=0)
    events_processed: int = Field(ge=0)
    warmed: bool
    last_frame: AwareDatetime | None = None
    signal_id: UUID | None = None
    bar_start: AwareDatetime | None = None
    decision_at: AwareDatetime | None = None
    execution_eligible: bool = False
    missing_members: int = Field(default=0, ge=0)
    weights: dict[str, Decimal] = Field(default_factory=dict)
    inputs: tuple[SignalInput, ...] = ()
    warmup: tuple[dict, ...] = ()


def enqueue_targets(
    session: Session, run: m.StrategyRun, spec: StrategyRunSpec, emission: StrategyEmission
) -> None:
    assert (
        emission.signal_id is not None and emission.decision_at is not None and emission.bar_start is not None
    )
    symbols = [r.symbol for r in emission.inputs if str(r.instrument_id) in emission.weights]
    if not symbols:
        return
    older = (
        select(m.TradingCommand.id)
        .where(
            m.TradingCommand.account_id == spec.account_id,
            m.TradingCommand.action == "target",
            m.TradingCommand.state == "queued",
            m.TradingCommand.payload["strategy_id"].astext == str(run.id),
            m.TradingCommand.payload["symbol"].astext.in_(symbols),
        )
        .order_by(m.TradingCommand.created_at, m.TradingCommand.id)
        .limit(len(symbols))
        .with_for_update()
    )
    session.execute(
        update(m.TradingCommand)
        .where(m.TradingCommand.id.in_(older), m.TradingCommand.state == "queued")
        .values(
            state="complete",
            completed_at=func.clock_timestamp(),
            result={"reason": "target_superseded", "signal_id": str(emission.signal_id)},
        )
    )
    account = session.get(m.TradingAccount, spec.account_id)
    if account is None:
        raise ValueError("Paper strategy account disappeared")
    positions = account.attribution.get(str(run.id), {}).get("positions", {})
    outstanding = {s for s, p in positions.items() if Decimal(p["quantity"])}
    outstanding.update(
        session.scalars(
            select(m.OrderIntent.payload["symbol"].astext).where(
                m.OrderIntent.account_id == spec.account_id,
                m.OrderIntent.strategy_id == run.id,
                m.OrderIntent.reserved_notional > 0,
            )
        )
    )
    outstanding.update(
        session.scalars(
            select(m.StrategyTargetState.symbol).where(
                m.StrategyTargetState.account_id == spec.account_id,
                m.StrategyTargetState.strategy_id == run.id,
                m.StrategyTargetState.activation_at == emission.activation_at,
                m.StrategyTargetState.weight != 0,
            )
        )
    )
    for reference in emission.inputs:
        weight = emission.weights.get(str(reference.instrument_id))
        if weight is None or (not weight and reference.symbol not in outstanding):
            continue
        identity = uuid5(emission.signal_id, str(reference.instrument_id))
        payload = dict(
            run_id=str(spec.trading_run_id),
            strategy_id=str(run.id),
            decision_id=str(identity),
            decision_at=emission.decision_at.isoformat(),
            expires_at=(
                emission.bar_start
                + timedelta(minutes=1, seconds=spec.frame_delay_seconds + spec.decision_lifetime_seconds)
            ).isoformat(),
            symbol=reference.symbol,
            weight=str(weight),
            price=str(reference.price),
            signal_id=str(emission.signal_id),
            activation_at=emission.activation_at.isoformat(),
            rebalance_policy=spec.strategy.rebalance_policy,
        )
        session.execute(
            insert(m.TradingCommand)
            .values(
                id=identity,
                request_hash=digest_json((str(spec.account_id), "target", payload)),
                account_id=spec.account_id,
                action="target",
                payload=payload,
            )
            .on_conflict_do_nothing()
        )


def persist_emission(session: Session, raw: str) -> bool:
    emission = StrategyEmission.model_validate_json(raw)
    if emission.engine_id != strategy_engine_id():
        raise ValueError("Strategy emission belongs to a different engine")
    run = session.scalar(
        select(m.StrategyRun).where(m.StrategyRun.id == emission.run_id).with_for_update(key_share=True)
    )
    if run is None:
        raise ValueError("Strategy emission run does not exist")
    references = [*emission.inputs, *[SignalInput(**ref) for ref in emission.warmup]]
    # Financial admission may only see signals whose exact market inputs committed.
    for begin in range(0, len(references), 500):
        batch = references[begin : begin + 500]
        keys = {(r.series_id, r.bar_start, r.revision_id) for r in batch}
        found = set(
            session.execute(
                select(m.BarRevision.series_id, m.BarRevision.bar_start, m.BarRevision.event_id).where(
                    tuple_(m.BarRevision.series_id, m.BarRevision.bar_start, m.BarRevision.event_id).in_(keys)
                )
            ).all()
        )
        if len(found) != len(keys):
            return False
    for purpose, items in (
        ("frame", emission.inputs),
        ("warmup", [SignalInput(**ref) for ref in emission.warmup]),
    ):
        for begin in range(0, len(items), 500):
            session.execute(
                insert(m.StrategyInput)
                .values(
                    [
                        dict(
                            run_id=emission.run_id,
                            activation_at=emission.activation_at,
                            bar_start=r.bar_start,
                            series_id=r.series_id,
                            revision_id=r.revision_id,
                            purpose=purpose,
                        )
                        for r in items[begin : begin + 500]
                    ]
                )
                .on_conflict_do_nothing()
            )
    current = run.activated_at == emission.activation_at and run.engine_id == emission.engine_id
    advanced = current and (
        emission.events_processed >= run.cursor
        and emission.frames >= run.frames
        and emission.skipped_frames >= run.skipped_frames
        and emission.late_revisions >= run.late_revisions
    )
    if advanced:
        session.execute(
            update(m.StrategyRun)
            .where(m.StrategyRun.id == run.id)
            .values(
                checkpoint=emission.checkpoint,
                frames=emission.frames,
                skipped_frames=emission.skipped_frames,
                late_revisions=emission.late_revisions,
                warmed=emission.warmed,
                last_frame=emission.last_frame,
                cursor=emission.events_processed,
                reason="ready" if emission.warmed else "warming",
                updated_at=func.clock_timestamp(),
            )
        )
    if emission.signal_id:
        if emission.bar_start is None or emission.decision_at is None:
            raise ValueError("Strategy signal has no frame or decision time")
        refs = [
            dict(series_id=r.series_id, bar_start=r.bar_start.isoformat(), revision_id=str(r.revision_id))
            for r in emission.inputs
        ]
        created = session.scalar(
            insert(m.StrategySignal)
            .values(
                id=emission.signal_id,
                run_id=run.id,
                bar_start=emission.bar_start,
                decision_at=emission.decision_at,
                weights={k: str(v) for k, v in emission.weights.items()},
                inputs=refs,
                missing_members=emission.missing_members,
                execution_eligible=emission.execution_eligible,
                state_sha256=digest_json(emission.checkpoint),
                activation_at=emission.activation_at,
            )
            .on_conflict_do_nothing()
            .returning(m.StrategySignal.id)
        )
        if created and advanced and run.state == "running" and emission.execution_eligible:
            spec = StrategyRunSpec.model_validate(run.spec)
            if spec.mode == "paper":
                enqueue_targets(session, run, spec, emission)
        log.info(
            "strategy_signal_persisted",
            extra={"signal_id": str(emission.signal_id), "strategy_id": str(run.id)},
        )
    return True


class StrategyWorker:
    def __init__(
        self,
        engine: Engine,
        token: UUID,
        bus: MarketBus | None = None,
        *,
        run_ids: tuple[UUID, ...] | None = None,
    ):
        self.engine, self.token = engine, str(token)
        self.bus = bus or MarketBus.from_settings()
        self.run_ids = run_ids
        self.prefix = self.bus.settings.market_stream + ":actor:"
        raw = cast(str | None, self.bus.redis.get(self.prefix + "configuration"))
        self.configurations = [c for c in json.loads(raw or "[]") if c["engine_id"] == strategy_engine_id()]
        self.next_refresh = 0.0

    def _keys(self, config: dict) -> tuple[str, str, str]:
        name = config["id"] + ":" + config["activated_at"]
        return self.prefix + name + ":state", self.prefix + name + ":lease", "strategy-v2:" + name

    def _refresh(self) -> None:
        if time.monotonic() < self.next_refresh:
            return
        self.next_refresh = time.monotonic() + 2
        try:
            configurations = []
            with database_session(self.engine) as session:
                selected = select(m.StrategyRun).where(
                    m.StrategyRun.state == "running", m.StrategyRun.engine_id == strategy_engine_id()
                )
                if self.run_ids is not None:
                    selected = selected.where(m.StrategyRun.id.in_(self.run_ids))
                for run in session.scalars(selected):
                    config = to_jsonable_python(row_dict(run))
                    members = (
                        session.execute(
                            select(
                                m.StrategyMember.series_id,
                                m.StrategyMember.instrument_id,
                                m.StrategyMember.symbol,
                                m.InstrumentMapping.provider_instrument_id,
                            )
                            .join(m.Series, m.Series.id == m.StrategyMember.series_id)
                            .join(
                                m.InstrumentMapping,
                                and_(
                                    m.InstrumentMapping.instrument_id == m.StrategyMember.instrument_id,
                                    m.InstrumentMapping.source_id == m.Series.source_id,
                                    m.InstrumentMapping.valid_to.is_(None),
                                ),
                            )
                            .where(m.StrategyMember.run_id == run.id)
                        )
                        .mappings()
                        .all()
                    )
                    config["members"] = to_jsonable_python([dict(member) for member in members])
                    state_key, _, _ = self._keys(config)
                    if not self.bus.redis.exists(state_key):
                        config["warmup"] = self._warmup(session, run, config["members"])
                    configurations.append(config)
            live_keys = {self._keys(c)[0] for c in configurations}
            for old in self.configurations:
                state_key, lease_key, group = self._keys(old)
                if state_key not in live_keys:
                    self.bus.redis.delete(lease_key)
                    self.bus.redis.xgroup_destroy(self.bus.settings.market_stream, group)
            self.configurations = configurations
            self.bus.redis.set(self.prefix + "configuration", json.dumps(configurations))
        except SQLAlchemyError as error:
            # Cached Redis configuration/checkpoints let already bootstrapped
            # actors keep calculating while the database component is unavailable.
            log.warning("strategy_control_refresh_failed", extra={"error_type": type(error).__name__})

    def _warmup(self, session: Session, run: m.StrategyRun, members: list[dict]) -> list[dict]:
        assert run.activated_at is not None
        spec = StrategyRunSpec.model_validate(run.spec)
        length = (
            spec.strategy.slow + 1 if spec.strategy.name == "cross_section_momentum" else spec.strategy.slow
        )
        selected = {member["series_id"]: member for member in members}
        ranked = (
            select(
                m.BarRevision.envelope,
                m.Bar.series_id,
                m.Bar.bar_start,
                func.row_number()
                .over(partition_by=m.Bar.series_id, order_by=m.Bar.bar_start.desc())
                .label("rank"),
            )
            .join(
                m.BarRevision,
                and_(
                    m.BarRevision.series_id == m.Bar.series_id,
                    m.BarRevision.bar_start == m.Bar.bar_start,
                    m.BarRevision.event_id == m.Bar.revision_id,
                ),
            )
            .where(
                m.Bar.series_id.in_(selected),
                m.Bar.session == "regular",
                m.Bar.bar_start < run.activated_at.replace(second=0, microsecond=0),
                m.BarRevision.available_at <= func.clock_timestamp(),
            )
            .subquery()
        )
        return [
            dict(member=selected[row.series_id], envelope=row.envelope)
            for row in session.execute(
                select(ranked.c.envelope, ranked.c.series_id)
                .where(ranked.c.rank <= length)
                .order_by(ranked.c.bar_start, ranked.c.series_id)
            )
        ]

    def step(self) -> bool:
        self._refresh()
        worked = False
        for config in self.configurations:
            worked = self._step_run(config) or worked
        return worked

    def _step_run(self, config: dict) -> bool:
        state_key, lease_key, group = self._keys(config)
        acquired = self.bus.redis.set(lease_key, self.token, nx=True, px=30000)
        if not acquired:
            renewed = self.bus.redis.eval(
                "if redis.call('GET',KEYS[1])==ARGV[1] then "
                "return redis.call('PEXPIRE',KEYS[1],30000) else return 0 end",
                1,
                lease_key,
                self.token,
            )
            if not renewed:
                return False
        self.bus.group(group)
        raw = cast(str | None, self.bus.redis.get(state_key))
        if raw is None:
            groups = cast(list, self.bus.redis.xinfo_groups(self.bus.settings.market_stream))
            progress = next(g for g in groups if g["name"] == group)
            if progress["last-delivered-id"] != "0-0":
                raise RuntimeError("Strategy checkpoint is missing after delivery; recovery review required")
        spec = StrategyRunSpec.model_validate(config["spec"])
        initialized = not raw
        state: ActorState = (
            json.loads(raw)
            if raw
            else ActorState(
                checkpoint={},
                buffer={},
                last_frame=None,
                frames=0,
                skipped_frames=0,
                late_revisions=0,
                events_processed=0,
                warmed=True,
            )
        )
        runtime = StrategyRuntime(spec.strategy, state["checkpoint"])
        warm_refs = []
        if initialized:
            for _, frame in groupby(config.get("warmup", []), key=lambda r: r["envelope"]["payload"]["start"]):
                bars = []
                for item in frame:
                    member, envelope = item["member"], item["envelope"]
                    payload = BarPayload.model_validate(envelope["payload"])
                    bars.append(payload.bar(member["instrument_id"]))
                    warm_refs.append(
                        dict(
                            series_id=member["series_id"],
                            instrument_id=member["instrument_id"],
                            symbol=member["symbol"],
                            bar_start=payload.start.isoformat(),
                            revision_id=envelope["event_id"],
                            price=str(payload.close),
                        )
                    )
                runtime.on_frame(bars)
        deliveries = self.bus.deliveries(group, self.token, count=500)
        members = {r["provider_instrument_id"]: r for r in config["members"]}
        activation = datetime.fromisoformat(config["activated_at"])
        now = datetime.now(UTC)
        for _, fields in deliveries:
            state["events_processed"] += 1
            event = MarketEvent.model_validate_json(fields["event"])
            if (
                event.provider != spec.source
                or event.feed != spec.feed
                or event.provider_instrument_id not in members
                or event.payload.session != "regular"
            ):
                continue
            if event.payload.start < activation.replace(second=0, microsecond=0):
                continue
            if event.available_at > now:
                raise ValueError("Strategy received a future market event")
            stamp = event.payload.start.isoformat()
            if state["last_frame"] and stamp <= state["last_frame"]:
                state["late_revisions"] += 1
                continue
            frame = state["buffer"].setdefault(stamp, {})
            previous = frame.get(event.provider_instrument_id)
            priority = {"historical": 30, "live_updated": 20, "live": 10}
            if previous is None or (priority[event.origin], event.observed_at) > (
                priority[previous["origin"]],
                datetime.fromisoformat(previous["observed_at"].replace("Z", "+00:00")),
            ):
                frame[event.provider_instrument_id] = event.model_dump(mode="json")
        for stamp in sorted(state["buffer"])[: -spec.max_buffered_frames]:
            del state["buffer"][stamp]
            state["skipped_frames"] += 1
            state["last_frame"] = stamp
        signal: dict[str, Any] = {}
        closed = False
        groups = cast(list, self.bus.redis.xinfo_groups(self.bus.settings.market_stream))
        lag = next((g.get("lag") for g in groups if g["name"] == group), None)
        if state["buffer"]:
            stamp = min(state["buffer"])
            start = datetime.fromisoformat(stamp)
            if start + timedelta(minutes=1, seconds=spec.frame_delay_seconds) <= now and (
                lag == 0 or len(state["buffer"][stamp]) == len(members)
            ):
                closed = True
                entries = state["buffer"].pop(stamp)
                events = [MarketEvent.model_validate(e) for e in entries.values()]
                missing = len(members) - len(events)
                coverage = Decimal(len(events)) / Decimal(len(members))
                if (
                    not missing or spec.missing_policy == "available"
                ) and coverage >= spec.min_coverage_fraction:
                    weights = runtime.on_frame(
                        [e.payload.bar(members[e.provider_instrument_id]["instrument_id"]) for e in events]
                    )
                    identity = uuid5(UUID(config["id"]), activation.isoformat() + ":" + start.isoformat())
                    fresh = now < start + timedelta(
                        minutes=1, seconds=spec.frame_delay_seconds + spec.decision_lifetime_seconds
                    )
                    refs = [
                        SignalInput(
                            series_id=members[e.provider_instrument_id]["series_id"],
                            instrument_id=members[e.provider_instrument_id]["instrument_id"],
                            symbol=members[e.provider_instrument_id]["symbol"],
                            bar_start=start,
                            revision_id=e.event_id,
                            price=e.payload.close,
                        )
                        for e in events
                    ]
                    signal = dict(
                        signal_id=identity,
                        bar_start=start,
                        decision_at=now,
                        execution_eligible=fresh,
                        missing_members=missing,
                        weights={s: w * spec.capital_fraction for s, w in weights.items()},
                        inputs=refs,
                    )
                    state["frames"] += 1
                else:
                    state["skipped_frames"] += 1
                state["last_frame"] = stamp
        state["checkpoint"] = runtime.checkpoint()
        emission = None
        if initialized or closed or deliveries:
            emission = StrategyEmission(
                run_id=config["id"],
                activation_at=activation,
                engine_id=strategy_engine_id(),
                checkpoint=state["checkpoint"],
                frames=state["frames"],
                skipped_frames=state["skipped_frames"],
                late_revisions=state["late_revisions"],
                events_processed=state["events_processed"],
                warmed=state["warmed"],
                last_frame=datetime.fromisoformat(state["last_frame"]) if state["last_frame"] else None,
                warmup=tuple(warm_refs),
                **signal,
            )
        if not initialized and not deliveries and not closed:
            return False
        if not self.bus.commit_actor(
            state_key,
            lease_key,
            self.token,
            raw,
            state,
            group,
            deliveries,
            emission.model_dump_json() if emission else None,
        ):
            return False
        if signal:
            log.info(
                "strategy_signal_emitted",
                extra={
                    "strategy_id": config["id"],
                    "signal_id": str(signal["signal_id"]),
                    "inputs": len(signal["inputs"]),
                },
            )
        return bool(initialized or deliveries or closed)
