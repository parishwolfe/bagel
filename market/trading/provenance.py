"""Resolve immutable strategy evidence through mapped ORM entities."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from market import models as m
from market.strategies.service import strategy_engine_id


def signal_context(
    session: Session,
    account_id: UUID,
    strategy_id: UUID | None,
    run_id: UUID,
    signal_id: UUID,
    symbol: str,
    decision_at: datetime | None,
    feed: str | None,
) -> tuple[str | None, dict]:
    evidence: dict = {"signal_id": str(signal_id)}
    signal = session.get(m.StrategySignal, signal_id)
    run = session.get(m.StrategyRun, strategy_id) if strategy_id else None
    member = session.scalar(
        select(m.StrategyMember).where(
            m.StrategyMember.run_id == strategy_id, m.StrategyMember.symbol == symbol
        )
    )
    if not signal or not run or signal.run_id != strategy_id or not member:
        return "strategy_signal_not_found", evidence
    spec = run.spec
    if (
        run.state != "running"
        or run.engine_id != strategy_engine_id()
        or spec.get("mode") != "paper"
        or spec.get("account_id") != str(account_id)
        or spec.get("trading_run_id") != str(run_id)
        or spec.get("source") != "alpaca"
        or spec.get("feed") != feed
    ):
        return "strategy_signal_run_mismatch", evidence
    if (
        not decision_at
        or signal.activation_at is None
        or signal.activation_at != run.activated_at
        or signal.decision_at != decision_at
    ):
        return "strategy_signal_activation_mismatch", evidence
    weight = signal.weights.get(str(member.instrument_id))
    if not signal.execution_eligible or weight is None:
        return "strategy_signal_not_executable", evidence
    newer = session.scalar(
        select(m.StrategySignal.id)
        .where(
            m.StrategySignal.run_id == strategy_id,
            m.StrategySignal.activation_at == signal.activation_at,
            m.StrategySignal.bar_start > signal.bar_start,
            m.StrategySignal.execution_eligible,
            m.StrategySignal.weights.has_key(str(member.instrument_id)),
        )
        .limit(1)
    )
    if newer:
        return "strategy_signal_superseded", evidence
    reference = session.get(
        m.StrategyInput, (strategy_id, signal.activation_at, signal.bar_start, member.series_id, "frame")
    )
    revision = (
        session.get(m.BarRevision, (member.series_id, signal.bar_start, reference.revision_id))
        if reference
        else None
    )
    if revision is None or revision.available_at > decision_at:
        return "input_revision_unavailable_at_decision", evidence
    evidence.update(
        activation_at=signal.activation_at,
        decision_at=decision_at,
        bar_start=signal.bar_start,
        series_id=member.series_id,
        revision_id=str(revision.event_id),
        weight=weight,
        price=revision.envelope["payload"]["close"],
        rebalance_policy=spec.get("strategy", {}).get("rebalance_policy", "on_weight_change"),
    )
    return None, evidence
