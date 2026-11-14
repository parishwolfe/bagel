"""Account-wide serialized exposure admission. No broker I/O occurs in this module."""

from datetime import timedelta
from decimal import Decimal
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from market import models as m
from market.trading.contracts import OrderCommand, RiskPolicy
from market.trading.provenance import signal_context
from market.trading.repository import audit, locked_account

ZERO = Decimal(0)


def minimum_quantity_change(side: str, quantity: Decimal, order_class: str) -> Decimal:
    delta = quantity if side == "buy" else -quantity
    if order_class == "bracket":
        return -quantity  # Entry plus a possible double-exit fill race.
    return min(ZERO, delta * (2 if order_class == "oco" else 1))


def evaluate(
    policy: RiskPolicy,
    account: dict,
    command: OrderCommand,
    asset: dict,
    market: dict,
    reservations: list[dict],
    now,
) -> tuple[str | None, Decimal, dict]:
    order = command.order
    snapshot = account["snapshot"]
    evidence = {
        "snapshot": snapshot,
        "positions": account["positions"],
        "reservations": reservations,
        "market": market,
        "asset_id": asset.get("id"),
        "order": order.model_dump(mode="json"),
    }
    if account["paused"] or not account["armed_until"] or account["armed_until"] <= now:
        return "account_paused", ZERO, evidence
    if str(account["run_id"]) != str(command.run_id):
        return "run_not_armed", ZERO, evidence
    if not account["reconciled"] or not account["stream_connected"]:
        return "account_unreconciled", ZERO, evidence
    if (
        not account["reconciled_at"]
        or (now - account["reconciled_at"]).total_seconds() > policy.account_fresh_seconds
    ):
        return "account_stale", ZERO, evidence
    if snapshot.get("trading_blocked") or snapshot.get("status") != "ACTIVE":
        return "broker_account_blocked", ZERO, evidence
    if asset.get("class") != "us_equity" or asset.get("status") != "active" or not asset.get("tradable"):
        return "asset_not_tradable", ZERO, evidence
    if not market:
        return "market_input_missing", ZERO, evidence
    if market["feed"] != snapshot.get("feed") or market["adjustment"] != "raw":
        return "feed_or_adjustment_mismatch", ZERO, evidence
    price = Decimal(market["close"])
    if not price.is_finite() or price <= 0:
        return "invalid_market_price", ZERO, evidence
    session_open = snapshot.get("market_open", False)
    if not session_open and policy.session_policy == "regular":
        return "outside_regular_session", ZERO, evidence
    freshness_reference = now
    if not session_open and policy.session_policy == "queue_next_regular":
        freshness_reference = market["previous_close"]
        if order.extended_hours:
            return "queued_session_cannot_execute_extended", ZERO, evidence
    if (freshness_reference - market["bar_end"]).total_seconds() > policy.market_fresh_seconds:
        return "market_data_stale", ZERO, evidence
    if market["bar_end"] > now:
        return "market_input_not_completed", ZERO, evidence
    if not session_open and policy.session_policy == "extended" and not order.extended_hours:
        return "extended_order_required", ZERO, evidence
    if order.qty is None:
        assert order.notional is not None
        quantity = order.notional / price
    else:
        quantity = order.qty
    if quantity != quantity.to_integral_value() and not asset.get("fractionable"):
        return "asset_not_fractionable", ZERO, evidence
    positions = account["positions"]
    held = Decimal(positions.get(order.symbol, {}).get("quantity", "0"))
    delta = quantity * (1 if order.side == "buy" else -1)
    symbol_pending = [r for r in reservations if r["symbol"] == order.symbol]
    reducing = (
        order.qty is not None
        and held * delta < 0
        and abs(delta) <= abs(held)
        and not symbol_pending
        and order.order_class == "simple"
    )
    if any(r.get("quantity") is None for r in symbol_pending):
        return "pending_quantity_uncertain", ZERO, evidence
    pending_minimum = sum(
        (
            minimum_quantity_change(r["side"], Decimal(r["quantity"]), r.get("order_class") or "simple")
            for r in symbol_pending
        ),
        ZERO,
    )
    minimum_quantity = (
        held + pending_minimum + minimum_quantity_change(order.side, quantity, order.order_class)
    )
    potential_short = minimum_quantity < min(held, ZERO)
    if potential_short and (order.notional is not None or quantity != quantity.to_integral_value()):
        return "provider_fractional_short_unsupported", ZERO, evidence
    if potential_short and not (asset.get("shortable") and asset.get("easy_to_borrow")):
        return "borrow_unavailable", ZERO, evidence
    if potential_short and not snapshot.get("shorting_enabled", False):
        return "account_shorting_disabled", ZERO, evidence
    price = max(
        price,
        order.limit_price or ZERO,
        order.stop_price or ZERO,
        order.take_profit.limit_price if order.take_profit else ZERO,
        order.stop_loss.stop_price if order.stop_loss else ZERO,
        (order.stop_loss.limit_price or ZERO) if order.stop_loss else ZERO,
    )
    buffered = price * (1 + policy.price_buffer_bps / Decimal(10000))
    multiplier = 3 if order.order_class == "bracket" else 2 if order.order_class in ("oto", "oco") else 1
    reservation = quantity * buffered * multiplier
    equity = Decimal(snapshot["equity"])
    if not equity.is_finite() or equity <= 0:
        return "no_equity", ZERO, evidence
    reserved = sum((Decimal(r["notional"]) for r in reservations), ZERO)
    exposures: dict[str, Decimal] = {}
    for symbol, position in positions.items():
        mark = snapshot.get("marks", {}).get(symbol)
        if mark is None:
            return "position_mark_missing", ZERO, evidence
        mark = Decimal(mark)
        if not mark.is_finite() or mark <= 0:
            return "position_mark_invalid", ZERO, evidence
        mark_time = snapshot.get("mark_times", {}).get(symbol)
        if mark_time is None:
            return "position_mark_unverified", ZERO, evidence
        from datetime import datetime

        mark_at = datetime.fromisoformat(mark_time)
        if mark_at > now or (freshness_reference - mark_at).total_seconds() > policy.market_fresh_seconds:
            return "position_mark_stale", ZERO, evidence
        exposures[symbol] = Decimal(position["quantity"]) * (
            max(mark, price) if symbol == order.symbol else mark
        )
    gross = sum(map(abs, exposures.values()), ZERO)
    net = sum(exposures.values(), ZERO)
    if not reducing:
        if any(abs(v) > equity * policy.max_position_fraction for v in exposures.values()):
            return "existing_position_limit", ZERO, evidence
        if account["session_equity"] and equity < account["session_equity"] * (
            1 - policy.max_daily_loss_fraction
        ):
            return "daily_loss_limit", ZERO, evidence
        if account["equity_peak"] and equity < account["equity_peak"] * (1 - policy.max_drawdown_fraction):
            return "drawdown_limit", ZERO, evidence
        symbol_reserved = sum((Decimal(r["notional"]) for r in symbol_pending), ZERO)
        if (
            abs(exposures.get(order.symbol, ZERO)) + symbol_reserved + reservation
            > equity * policy.max_position_fraction
        ):
            return "position_limit", ZERO, evidence
        if gross + reserved + reservation > equity * policy.max_gross_fraction:
            return "gross_limit", ZERO, evidence
        if abs(net) + reserved + reservation > equity * policy.max_net_fraction:
            return "net_limit", ZERO, evidence
        if reserved + reservation > Decimal(snapshot["buying_power"]):
            return "buying_power", ZERO, evidence
        short = -sum((v for v in exposures.values() if v < 0), ZERO)
        projected_short = short + reserved + (reservation if potential_short else ZERO)
        if projected_short > equity * policy.max_short_fraction:
            return "short_limit", ZERO, evidence
        required = projected_short * max(policy.initial_short_margin, policy.maintenance_short_margin)
        if projected_short and equity - required < policy.min_margin_buffer:
            return "short_margin", ZERO, evidence
    evidence.update(
        {
            "quantity": str(quantity),
            "risk_price": str(buffered),
            "reducing": reducing,
            "minimum_quantity": str(minimum_quantity),
            "increases_short": potential_short,
            "gross": str(gross),
            "net": str(net),
            "reserved": str(reserved),
        }
    )
    return None, reservation, evidence


def admit(
    conn: Session,
    account_id: UUID,
    token: UUID,
    identity: UUID,
    command: OrderCommand,
    asset: dict,
    market: dict,
    created_at,
    *,
    target_sizing: dict | None = None,
) -> dict:
    account = locked_account(conn, account_id, token)
    now = account["now"]
    decision_id = command.decision_id or identity
    existing = conn.scalar(
        select(m.OrderIntent).where(
            m.OrderIntent.account_id == account_id, m.OrderIntent.decision_id == decision_id
        )
    )
    if existing:
        return {"id": existing.id, "state": existing.state, "reason": existing.reason}
    policy = RiskPolicy.model_validate(account["policy"])
    decision_at = command.decision_at or created_at
    expires_at = min(
        command.expires_at or decision_at + timedelta(seconds=policy.decision_lifetime_seconds),
        decision_at + timedelta(seconds=policy.decision_lifetime_seconds),
    )
    reservations = [
        {
            "symbol": r.payload["symbol"],
            "notional": str(r.reserved_notional),
            "quantity": r.payload.get("qty"),
            "side": r.payload["side"],
            "order_class": r.payload.get("order_class"),
        }
        for r in conn.scalars(
            select(m.OrderIntent).where(
                m.OrderIntent.account_id == account_id, m.OrderIntent.reserved_notional > 0
            )
        )
    ]
    reason, reservation, inputs = evaluate(policy, account, command, asset, market, reservations, now)
    if target_sizing is not None:
        inputs["target_sizing"] = target_sizing
    references = [ref.model_dump(mode="json") for ref in command.inputs] or (
        [market["reference"]] if market else []
    )
    if command.signal_id:
        signal_reason, signal = signal_context(
            conn,
            account_id,
            command.strategy_id,
            command.run_id,
            command.signal_id,
            command.order.symbol,
            command.decision_at,
            account["snapshot"].get("feed"),
        )
        inputs["strategy_signal"] = signal
        if signal_reason:
            reason = signal_reason
            references = []
        else:
            references = [
                {
                    "series_id": signal["series_id"],
                    "bar_start": signal["bar_start"].isoformat(),
                    "revision_id": signal["revision_id"],
                }
            ]
    if references:
        from datetime import datetime

        for reference in references:
            revision = conn.get(
                m.BarRevision,
                (
                    reference["series_id"],
                    datetime.fromisoformat(reference["bar_start"]),
                    UUID(reference["revision_id"]),
                ),
            )
            if revision is None or revision.available_at > decision_at:
                reason = "input_revision_unavailable_at_decision"
                break
    if decision_at > now + timedelta(seconds=1):
        reason = "future_decision"
    if expires_at <= now:
        reason = "decision_expired"
    state = "rejected" if reason else "admitted"
    payload = command.order.broker_payload("bgl-" + identity.hex)
    signal_id = (
        command.signal_id if command.signal_id and conn.get(m.StrategySignal, command.signal_id) else None
    )
    conn.execute(
        insert(m.OrderIntent).values(
            id=identity,
            account_id=account_id,
            decision_id=decision_id,
            strategy_id=command.strategy_id,
            run_id=command.run_id,
            client_order_id=payload["client_order_id"],
            decision_at=decision_at,
            expires_at=expires_at,
            payload=payload,
            inputs=references,
            state=state,
            reserved_notional=ZERO if reason else reservation,
            reason=reason,
            signal_id=signal_id,
        )
    )
    # datetime values are normalized explicitly, never serialized as SDK objects.
    from pydantic_core import to_jsonable_python

    evidence = to_jsonable_python(inputs)
    conn.execute(
        insert(m.RiskDecision).values(
            intent_id=identity,
            approved=not reason,
            policy_version=policy.version,
            policy=policy.model_dump(mode="json"),
            inputs=evidence,
            reason=reason or "approved",
            reservation=ZERO if reason else reservation,
        )
    )
    audit(
        conn,
        account_id,
        "risk_decision",
        {
            "intent_id": str(identity),
            "reason": reason or "approved",
            "reservation": str(ZERO if reason else reservation),
            "signal_id": str(command.signal_id) if command.signal_id else None,
        },
        uuid5(identity, "risk"),
    )
    return {"id": identity, "state": state, "reason": reason}
