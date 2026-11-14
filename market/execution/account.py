"""Fenced ORM account owner. Broker I/O never runs inside a database transaction."""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID, uuid4, uuid5

from pydantic_core import to_jsonable_python
from sqlalchemy import DateTime, Numeric, Uuid, and_, case, cast, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from websockets.exceptions import ConnectionClosed

from market import models as m
from market.data.domain import NEW_YORK
from market.database import database_session, row_dict
from market.events.journal import Journal
from market.execution.alpaca import (
    AmbiguousSubmission,
    BrokerRejected,
    BrokerUnavailable,
    PaperREST,
    PaperSettings,
    trading_updates,
)
from market.execution.monitor import ORDER_FIELDS, POSITION_FIELDS, save_event
from market.portfolio.accounting import AccountLedger
from market.portfolio.recovery import apply_attribution, rebuild
from market.portfolio.targets import target_quantity
from market.research.artifacts import digest_json
from market.risk.admission import admit, evaluate
from market.trading.contracts import (
    AccountView,
    ArmCommand,
    BrokerClockView,
    BrokerOrderView,
    BrokerPositionView,
    CancelCommand,
    OrderCommand,
    RiskPolicy,
    TargetCommand,
)
from market.trading.provenance import signal_context
from market.trading.repository import audit, complete_command, locked_account

log = logging.getLogger(__name__)
TERMINAL = ("filled", "canceled", "expired", "rejected", "replaced")


class AccountingBlocked(Exception):
    pass


def timestamp(value: str | datetime) -> datetime:
    result = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise BrokerUnavailable("Broker timestamp lacks timezone")
    return result.astimezone(UTC)


def store_order(
    session: Session, account_id: UUID, raw: dict, root_id: UUID | None = None, parent_id: UUID | None = None
) -> None:
    validated = BrokerOrderView.model_validate(raw)
    root = root_id or session.scalar(
        select(m.OrderIntent.id).where(
            m.OrderIntent.account_id == account_id, m.OrderIntent.client_order_id == raw["client_order_id"]
        )
    )
    if root is None and raw.get("group_id"):
        root = session.scalar(
            select(m.BrokerOrder.root_intent)
            .where(
                m.BrokerOrder.account_id == account_id,
                m.BrokerOrder.payload["group_id"].astext == str(raw["group_id"]),
                m.BrokerOrder.root_intent.is_not(None),
            )
            .limit(1)
        )
    previous = session.get(m.BrokerOrder, validated.id)
    if root and previous and previous.root_intent and previous.root_intent != root:
        raise BrokerUnavailable("Broker order ownership changed unexpectedly")
    body = {key: raw.get(key) for key in (*ORDER_FIELDS, "group_id", "group_role", "order_class", "side")}
    stamp = timestamp(raw.get("updated_at") or raw["created_at"])
    values: dict = dict(
        account_id=account_id,
        root_intent=root,
        client_order_id=raw["client_order_id"],
        symbol=raw["symbol"],
        state=raw["status"],
        quantity=Decimal(raw["qty"]) if raw.get("qty") else None,
        filled_quantity=Decimal(raw["filled_qty"]),
        updated_at=stamp,
        payload=body,
        parent_order_id=parent_id,
    )
    if previous is None:
        session.execute(insert(m.BrokerOrder).values(id=validated.id, **values))
    elif stamp >= previous.updated_at:
        values["root_intent"] = previous.root_intent or root
        values["parent_order_id"] = previous.parent_order_id or parent_id
        values["filled_quantity"] = max(previous.filled_quantity, values["filled_quantity"])
        if previous.state in TERMINAL and raw["status"] not in TERMINAL:
            values["state"] = previous.state
        session.execute(update(m.BrokerOrder).where(m.BrokerOrder.id == validated.id).values(**values))
    if root:
        if previous is None or previous.root_intent is None:
            applied = session.scalar(
                select(m.AccountActivity.activity_id)
                .where(
                    m.AccountActivity.account_id == account_id,
                    m.AccountActivity.applied,
                    m.AccountActivity.activity_type == "FILL",
                    m.AccountActivity.payload["order_id"].astext == str(validated.id),
                )
                .limit(1)
            )
            if applied:
                session.execute(
                    update(m.TradingAccount)
                    .where(m.TradingAccount.account_id == account_id)
                    .values(rebuild_required=True)
                )
        session.execute(
            update(m.OrderIntent)
            .where(m.OrderIntent.id == root)
            .values(
                broker_order_id=func.coalesce(m.OrderIntent.broker_order_id, validated.id),
                state=case(
                    (m.OrderIntent.state.in_(("dispatching", "unresolved")), "accepted"),
                    else_=m.OrderIntent.state,
                ),
                reason=None,
                updated_at=func.clock_timestamp(),
            )
        )
    for child in raw.get("legs") or ():
        if parent_id is not None:
            raise BrokerUnavailable("Unexpected nested broker order depth")
        store_order(session, account_id, child, root, validated.id)


class AccountWorker:
    def __init__(
        self,
        settings: PaperSettings,
        journal: Journal,
        transport_factory: Callable[[PaperSettings], PaperREST] = PaperREST,
    ):
        self.settings, self.journal, self.engine = settings, journal, journal.engine
        self.transport_factory = transport_factory
        self.broker = transport_factory(settings)
        self.token, self.account_id = uuid4(), None

    def initialize(self) -> None:
        account = AccountView.model_validate(self.broker.get("/v2/account"))
        self.account_id = account.id
        with database_session(self.engine) as session:
            statement = insert(m.TradingAccount).values(
                account_id=account.id, owner_token=self.token, policy=RiskPolicy().model_dump(mode="json")
            )
            session.execute(
                statement.on_conflict_do_update(
                    index_elements=[m.TradingAccount.account_id],
                    set_={
                        "owner_token": self.token,
                        "epoch": m.TradingAccount.epoch + 1,
                        "paused": True,
                        "run_id": None,
                        "armed_until": None,
                        "reconciled": False,
                        "stream_connected": False,
                        "reason": "startup_reconciliation",
                        "updated_at": func.clock_timestamp(),
                    },
                )
            )
            session.execute(
                insert(m.BrokerSyncCheckpoint).values(account_id=account.id).on_conflict_do_nothing()
            )
            session.execute(
                insert(m.PaperAccount)
                .values(
                    account_id=account.id,
                    status=account.status,
                    cash=account.cash,
                    equity=account.equity,
                    buying_power=account.buying_power,
                    trading_blocked=account.trading_blocked,
                    positions=[],
                    orders=[],
                    snapshot_at=func.clock_timestamp(),
                )
                .on_conflict_do_nothing()
            )
            session.execute(
                update(m.OrderIntent)
                .where(m.OrderIntent.account_id == account.id, m.OrderIntent.state == "dispatching")
                .values(state="unresolved", reason="interrupted_dispatch")
            )
            audit(
                session,
                account.id,
                "owner_started",
                {"owner_token": str(self.token), "submission_armed": False},
            )

    def block(self, reason: str, connected: bool | None = None) -> None:
        assert self.account_id
        with database_session(self.engine) as session:
            locked_account(session, self.account_id, self.token)
            values = dict(reconciled=False, reason=reason, updated_at=func.clock_timestamp())
            if connected is not None:
                values["stream_connected"] = connected
            session.execute(
                update(m.TradingAccount).where(m.TradingAccount.account_id == self.account_id).values(**values)
            )

    def sync_activities(self) -> bool:
        assert self.account_id
        for _ in range(self.settings.sync_pages):
            with database_session(self.engine) as session:
                progress = session.get(m.BrokerSyncCheckpoint, self.account_id)
                assert progress
                cursor, after = progress.activity_cursor, progress.activity_after
            rows = self.broker.activity_page(cursor, after.isoformat() if after else None)
            with database_session(self.engine) as session:
                locked_account(session, self.account_id, self.token)
                for raw in rows:
                    kind = raw["activity_type"]
                    if kind == "FILL":
                        effective = timestamp(raw["transaction_time"])
                        body = {
                            key: raw[key]
                            for key in (
                                "activity_type",
                                "id",
                                "order_id",
                                "symbol",
                                "side",
                                "qty",
                                "price",
                                "transaction_time",
                            )
                        }
                    else:
                        effective = datetime.combine(date.fromisoformat(raw["date"][:10]), time.min, NEW_YORK)
                        body = {
                            key: raw.get(key)
                            for key in ("activity_type", "id", "net_amount", "symbol", "qty", "date")
                        }
                    checksum = digest_json(body)
                    session.execute(
                        insert(m.AccountActivity)
                        .values(
                            account_id=self.account_id,
                            activity_id=raw["id"],
                            activity_type=kind,
                            effective_at=effective,
                            payload=body,
                            payload_sha256=checksum,
                        )
                        .on_conflict_do_nothing()
                    )
                    previous = session.get(m.AccountActivity, (self.account_id, raw["id"]))
                    if previous is None or previous.payload_sha256 != checksum:
                        raise BrokerUnavailable(
                            "An existing broker activity was revised; operator review required"
                        )
                exhausted = len(rows) < 100
                next_cursor = None if exhausted else str(rows[-1]["id"])
                if next_cursor and next_cursor == cursor:
                    raise BrokerUnavailable("Activity pagination did not advance")
                values: dict = dict(activity_cursor=next_cursor)
                if exhausted:
                    values.update(
                        activity_after=datetime.now(UTC) - timedelta(days=7), initial_activities_complete=True
                    )
                session.execute(
                    update(m.BrokerSyncCheckpoint)
                    .where(m.BrokerSyncCheckpoint.account_id == self.account_id)
                    .values(**values)
                )
            if exhausted:
                return True
        return False

    def sync_orders(self) -> bool:
        assert self.account_id
        with database_session(self.engine) as session:
            progress = session.get(m.BrokerSyncCheckpoint, self.account_id)
            assert progress
            cursor = progress.order_cursor
        exhausted = False
        for _ in range(self.settings.sync_pages):
            rows = self.broker.order_page("all", cursor)
            with database_session(self.engine) as session:
                locked_account(session, self.account_id, self.token)
                for row in rows:
                    store_order(session, self.account_id, row)
                exhausted = len(rows) < 500
                new_cursor = str(rows[-1]["id"]) if rows else cursor
                if rows and new_cursor == cursor and not exhausted:
                    raise BrokerUnavailable("Order pagination did not advance")
                cursor = new_cursor
                values: dict = dict(order_cursor=cursor)
                if exhausted:
                    values["initial_orders_complete"] = True
                session.execute(
                    update(m.BrokerSyncCheckpoint)
                    .where(m.BrokerSyncCheckpoint.account_id == self.account_id)
                    .values(**values)
                )
            if exhausted:
                break
        cursor, open_exhausted = None, False
        for _ in range(self.settings.sync_pages):
            rows = self.broker.order_page("open", cursor)
            with database_session(self.engine) as session:
                locked_account(session, self.account_id, self.token)
                for row in rows:
                    store_order(session, self.account_id, row)
            if len(rows) < 500:
                open_exhausted = True
                break
            next_cursor = str(rows[-1]["id"])
            if next_cursor == cursor:
                raise BrokerUnavailable("Open order pagination did not advance")
            cursor = next_cursor
        with database_session(self.engine) as session:
            pending = session.scalars(
                select(m.OrderIntent.client_order_id)
                .where(
                    m.OrderIntent.account_id == self.account_id,
                    m.OrderIntent.state.in_(("dispatching", "unresolved", "accepted")),
                )
                .order_by(m.OrderIntent.updated_at)
                .limit(self.settings.command_batch)
            ).all()
        for client_id in pending:
            order = self.broker.lookup_client_order(client_id)
            if order is not None:
                with database_session(self.engine) as session:
                    locked_account(session, self.account_id, self.token)
                    store_order(session, self.account_id, order)
                    session.execute(
                        update(m.OrderIntent)
                        .where(m.OrderIntent.client_order_id == client_id)
                        .values(updated_at=func.clock_timestamp())
                    )
        with database_session(self.engine) as session:
            order_id = m.AccountActivity.payload["order_id"].astext
            unmatched = session.scalars(
                select(order_id)
                .outerjoin(m.BrokerOrder, m.BrokerOrder.id == cast(order_id, Uuid))
                .where(
                    m.AccountActivity.account_id == self.account_id, m.AccountActivity.activity_type == "FILL"
                )
                .group_by(order_id, m.BrokerOrder.id, m.BrokerOrder.filled_quantity)
                .having(
                    or_(
                        m.BrokerOrder.id.is_(None),
                        func.sum(cast(m.AccountActivity.payload["qty"].astext, Numeric))
                        != m.BrokerOrder.filled_quantity,
                    )
                )
                .limit(self.settings.command_batch)
            ).all()
        for reference in unmatched:
            order = self.broker.get(f"/v2/orders/{UUID(reference)}", {"nested": "true"})
            if order is not None:
                with database_session(self.engine) as session:
                    locked_account(session, self.account_id, self.token)
                    store_order(session, self.account_id, order)
        return exhausted and open_exhausted

    def book(
        self, session: Session, account: dict, broker_account: AccountView, positions: list[dict]
    ) -> str | None:
        assert self.account_id
        pending = session.scalars(
            select(m.AccountActivity)
            .where(m.AccountActivity.account_id == self.account_id, ~m.AccountActivity.applied)
            .order_by(m.AccountActivity.effective_at, m.AccountActivity.activity_id)
            .limit(2000)
        ).all()
        if account["opening_cash"] is None:
            count = session.scalar(
                select(func.count())
                .select_from(m.AccountActivity)
                .where(m.AccountActivity.account_id == self.account_id)
            )
            orders = session.scalar(
                select(func.count())
                .select_from(m.BrokerOrder)
                .where(m.BrokerOrder.account_id == self.account_id)
            )
            if not count and not positions and not orders:
                opening = broker_account.cash
            elif count:
                opening = Decimal(0)
            else:
                return "opening_balance_requires_review"
            session.execute(
                update(m.TradingAccount)
                .where(m.TradingAccount.account_id == self.account_id)
                .values(opening_cash=opening, cash=opening)
            )
            account["cash"] = opening
            audit(
                session,
                self.account_id,
                "opening_balance",
                {"cash": str(opening), "basis": "confirmed_empty_or_full_history"},
            )
        ledger = AccountLedger.restore(
            account["cash"], account["positions"], account["realized_pnl"], account["fees"]
        )
        attribution = dict(account["attribution"])
        last = (
            (account["last_activity_time"], account["last_activity_id"])
            if account["last_activity_time"] is not None
            else None
        )
        if pending and last is not None and (pending[0].effective_at, pending[0].activity_id) < last:
            return "late_activity_requires_rebuild"
        for entry in pending:
            raw = entry.payload
            try:
                symbol, quantity, price, cash_delta, realized = ledger.apply(raw)
            except (ValueError, KeyError, ArithmeticError):
                return "unsupported_account_activity"
            key = "external"
            if raw["activity_type"] == "FILL":
                owner = session.execute(
                    select(m.OrderIntent.strategy_id)
                    .join(m.BrokerOrder, m.BrokerOrder.root_intent == m.OrderIntent.id)
                    .where(m.BrokerOrder.id == UUID(raw["order_id"]))
                ).first()
                key = str(owner[0]) if owner and owner[0] else "operator" if owner else "external"
            apply_attribution(attribution, key, raw)
            session.execute(
                insert(m.LedgerEntry)
                .values(
                    account_id=self.account_id,
                    activity_id=entry.activity_id,
                    attribution=key,
                    symbol=symbol,
                    quantity=quantity,
                    price=price,
                    cash_delta=cash_delta,
                    realized_pnl=realized,
                    effective_at=entry.effective_at,
                )
                .on_conflict_do_nothing()
            )
            session.execute(
                update(m.AccountActivity)
                .where(
                    m.AccountActivity.account_id == self.account_id,
                    m.AccountActivity.activity_id == entry.activity_id,
                )
                .values(applied=True)
            )
            last = (entry.effective_at, entry.activity_id)
            audit(
                session,
                self.account_id,
                "activity_applied",
                {"activity_id": entry.activity_id, "cash_delta": str(cash_delta), "realized": str(realized)},
                uuid5(self.account_id, "activity:" + entry.activity_id),
            )
        session.execute(
            update(m.TradingAccount)
            .where(m.TradingAccount.account_id == self.account_id)
            .values(
                cash=ledger.cash,
                positions=ledger.dump(),
                attribution=attribution,
                realized_pnl=ledger.realized,
                fees=ledger.fees,
                last_activity_time=last[0] if last else None,
                last_activity_id=last[1] if last else None,
            )
        )
        if len(pending) == 2000:
            return "accounting_backlog"
        expected = {s: p.quantity for s, p in ledger.positions.items() if p.quantity}
        actual = {p["symbol"]: Decimal(p["qty"]) for p in positions if Decimal(p["qty"])}
        if expected != actual:
            return "position_mismatch"
        return "cash_mismatch" if abs(ledger.cash - broker_account.cash) > Decimal(".01") else None

    def reconcile(self) -> bool:
        assert self.account_id
        self.block("reconciling")
        started = datetime.now(UTC)
        activities_complete, orders_complete = self.sync_activities(), self.sync_orders()
        account = AccountView.model_validate(self.broker.get("/v2/account"))
        positions, clock = self.broker.get("/v2/positions"), self.broker.get("/v2/clock")
        if not isinstance(positions, list) or not isinstance(clock, dict) or account.id != self.account_id:
            raise BrokerUnavailable("Invalid account reconciliation response")
        for position in positions:
            BrokerPositionView.model_validate(position)
        if len({p["symbol"] for p in positions}) != len(positions):
            raise BrokerUnavailable("Duplicate broker position evidence")
        broker_clock = BrokerClockView.model_validate(clock)
        clock_at = broker_clock.timestamp
        days = self.broker.get(
            "/v2/calendar",
            {
                "start": (clock_at.astimezone(NEW_YORK).date() - timedelta(days=10)).isoformat(),
                "end": clock_at.astimezone(NEW_YORK).date().isoformat(),
            },
        )
        if not isinstance(days, list):
            raise BrokerUnavailable("Invalid exchange calendar response")
        closes = [
            datetime.fromisoformat(d["date"] + "T" + d["close"]).replace(tzinfo=NEW_YORK).astimezone(UTC)
            for d in days
        ]
        previous_close = max((v for v in closes if v <= clock_at), default=clock_at)
        marks = {p["symbol"]: self.market_input(p["symbol"]) for p in positions}
        with database_session(self.engine) as session:
            state = locked_account(session, self.account_id, self.token)
            expired = session.scalars(
                select(m.OrderIntent.id)
                .where(
                    m.OrderIntent.account_id == self.account_id,
                    m.OrderIntent.state == "admitted",
                    m.OrderIntent.dispatch_count == 0,
                    m.OrderIntent.expires_at <= func.clock_timestamp(),
                )
                .order_by(m.OrderIntent.expires_at)
                .limit(1000)
                .with_for_update()
            ).all()
            if expired:
                session.execute(
                    update(m.OrderIntent)
                    .where(m.OrderIntent.id.in_(expired))
                    .values(state="expired", reserved_notional=0, reason="decision_expired")
                )
            for identity in expired:
                audit(
                    session,
                    self.account_id,
                    "decision_expired",
                    {"intent_id": str(identity)},
                    uuid5(identity, "expired"),
                )
            reason = None if activities_complete and orders_complete else "broker_history_backlog"
            if reason is None:
                try:
                    with session.begin_nested():
                        if state["rebuild_required"]:
                            raise AccountingBlocked("late_activity_requires_rebuild")
                        reason = self.book(session, state, account, positions)
                        if reason in (
                            "unsupported_account_activity",
                            "late_activity_requires_rebuild",
                            "opening_balance_requires_review",
                        ):
                            raise AccountingBlocked(reason)
                except AccountingBlocked as error:
                    reason = str(error)
                    if reason == "late_activity_requires_rebuild":
                        try:
                            with session.begin_nested():
                                rebuild(session, state, self.account_id)
                            reason = "portfolio_rebuilt_rechecking"
                        except (ValueError, KeyError, ArithmeticError):
                            reason = "unsupported_account_activity"
            unknown = session.scalar(
                select(m.BrokerOrder.id)
                .where(
                    m.BrokerOrder.account_id == self.account_id,
                    ~m.BrokerOrder.state.in_(TERMINAL),
                    m.BrokerOrder.root_intent.is_(None),
                )
                .limit(1)
            )
            unresolved = session.scalar(
                select(m.OrderIntent.id)
                .where(
                    m.OrderIntent.account_id == self.account_id,
                    m.OrderIntent.state.in_(("dispatching", "unresolved")),
                )
                .limit(1)
            )
            booked = (
                select(func.coalesce(func.sum(cast(m.AccountActivity.payload["qty"].astext, Numeric)), 0))
                .where(
                    m.AccountActivity.account_id == m.BrokerOrder.account_id,
                    m.AccountActivity.activity_type == "FILL",
                    m.AccountActivity.payload["order_id"].astext == cast(m.BrokerOrder.id, m.Text),
                    m.AccountActivity.applied,
                )
                .correlate(m.BrokerOrder)
                .scalar_subquery()
            )
            missing = session.scalar(
                select(m.BrokerOrder.id)
                .where(m.BrokerOrder.account_id == self.account_id, m.BrokerOrder.filled_quantity != booked)
                .limit(1)
            )
            changed = session.scalar(
                select(m.PaperStreamEvidence.event_id)
                .where(
                    m.PaperStreamEvidence.account_id == self.account_id,
                    m.PaperStreamEvidence.observed_at >= started,
                )
                .limit(1)
            )
            if not state["stream_connected"]:
                reason = "trading_stream_disconnected"
            elif account.trading_blocked or account.status != "ACTIVE":
                reason = "broker_account_blocked"
            elif unknown:
                reason = "unknown_open_broker_order"
            elif unresolved:
                reason = "unresolved_submission"
            elif missing:
                reason = "fill_activity_pending"
            elif changed:
                reason = "updates_during_snapshot"
            elif abs((datetime.now(UTC) - clock_at).total_seconds()) > 30:
                reason = "broker_clock_stale"
            snapshot = account.model_dump(mode="json")
            snapshot.update(
                market_open=broker_clock.is_open,
                clock_at=clock_at.isoformat(),
                previous_close=previous_close.isoformat(),
                feed=self.settings.market_feed,
                runtime_submission_enabled=self.settings.submission_enabled,
                marks={
                    p["symbol"]: str(max(Decimal(p["current_price"]), Decimal(marks[p["symbol"]]["close"])))
                    for p in positions
                    if p.get("current_price") and marks[p["symbol"]]
                },
                mark_times={s: mark["bar_end"].isoformat() for s, mark in marks.items() if mark},
                mark_references={s: mark["reference"] for s, mark in marks.items() if mark},
            )
            today = clock_at.astimezone(NEW_YORK).date()
            values = dict(
                snapshot=snapshot,
                reconciled=reason is None,
                reason=reason or "ready",
                updated_at=func.clock_timestamp(),
                equity_peak=max(state["equity_peak"] or account.equity, account.equity),
                session_date=today,
            )
            if reason is None:
                values["reconciled_at"] = func.clock_timestamp()
            if state["session_date"] != today:
                values["session_equity"] = account.equity
            if not state["paused"] and state["armed_until"] and state["armed_until"] <= state["now"]:
                values["paused"] = True
                audit(session, self.account_id, "arm_expired", {"run_id": str(state["run_id"])})
            session.execute(
                update(m.TradingAccount).where(m.TradingAccount.account_id == self.account_id).values(**values)
            )
            if reason is None:
                for intent in session.scalars(
                    select(m.OrderIntent)
                    .where(m.OrderIntent.account_id == self.account_id, m.OrderIntent.state == "accepted")
                    .limit(1000)
                ):
                    evidence = session.scalars(
                        select(m.BrokerOrder).where(m.BrokerOrder.root_intent == intent.id)
                    ).all()
                    expected = {"bracket": 3, "oto": 2, "oco": 2}.get(
                        intent.payload.get("order_class", "simple"), 1
                    )
                    if sum(
                        b.id == intent.broker_order_id or b.parent_order_id == intent.broker_order_id
                        for b in evidence
                    ) >= expected and all(b.state in TERMINAL for b in evidence):
                        intent.state, intent.reserved_notional = "terminal", Decimal(0)
                        intent.updated_at = datetime.now(UTC)
            recent = session.scalars(
                select(m.BrokerOrder)
                .where(m.BrokerOrder.account_id == self.account_id)
                .order_by(m.BrokerOrder.updated_at.desc())
                .limit(500)
            ).all()
            statement = insert(m.PaperAccount).values(
                account_id=self.account_id,
                status=account.status,
                cash=account.cash,
                equity=account.equity,
                buying_power=account.buying_power,
                trading_blocked=account.trading_blocked,
                positions=[{k: p.get(k) for k in POSITION_FIELDS} for p in positions],
                orders=[b.payload for b in recent],
                snapshot_at=func.clock_timestamp(),
                reconciled=reason is None,
                reconciliation_reason=reason or "ready",
            )
            session.execute(
                statement.on_conflict_do_update(
                    index_elements=[m.PaperAccount.account_id],
                    set_={
                        k: getattr(statement.excluded, k)
                        for k in (
                            "status",
                            "cash",
                            "equity",
                            "buying_power",
                            "trading_blocked",
                            "positions",
                            "orders",
                            "snapshot_at",
                            "reconciled",
                            "reconciliation_reason",
                        )
                    },
                )
            )
        return reason is None

    def market_input(self, symbol: str) -> dict:
        from market.events.bus import MarketBus

        bus = MarketBus.from_settings(self.settings)
        try:
            event = bus.latest("alpaca", self.settings.market_feed, symbol)
        finally:
            bus.close()
        if event is None:
            return {}
        with database_session(self.engine) as session:
            account = session.get(m.TradingAccount, self.account_id)
            sid = session.scalar(
                select(m.Series.id)
                .join(m.Source, m.Source.id == m.Series.source_id)
                .join(
                    m.InstrumentMapping,
                    and_(
                        m.InstrumentMapping.instrument_id == m.Series.instrument_id,
                        m.InstrumentMapping.source_id == m.Source.id,
                    ),
                )
                .where(
                    m.Source.code == event.provider,
                    m.Series.feed == event.feed,
                    m.Series.adjustment == "raw",
                    m.Series.interval_seconds == 60,
                    m.InstrumentMapping.provider_instrument_id == event.provider_instrument_id,
                    m.InstrumentMapping.valid_to.is_(None),
                )
            )
            previous = account.snapshot.get("previous_close") if account else None
        if sid is None:
            return {}
        return dict(
            close=str(event.payload.close),
            bar_end=event.payload.end,
            feed=event.feed,
            adjustment="raw",
            previous_close=timestamp(previous) if previous else datetime.now(UTC),
            reference={
                "series_id": sid,
                "bar_start": event.payload.start.isoformat(),
                "revision_id": str(event.event_id),
            },
        )

    def process_command(self, controls_only: bool = False) -> bool:
        assert self.account_id
        with database_session(self.engine) as session:
            pending = (
                select(m.OrderIntent.id)
                .where(
                    m.OrderIntent.account_id == m.TradingCommand.account_id,
                    m.OrderIntent.strategy_id == cast(m.TradingCommand.payload["strategy_id"].astext, Uuid),
                    m.OrderIntent.payload["symbol"].astext == m.TradingCommand.payload["symbol"].astext,
                    m.OrderIntent.reserved_notional > 0,
                )
                .exists()
            )
            statement = select(m.TradingCommand).where(
                m.TradingCommand.account_id == self.account_id,
                m.TradingCommand.state == "queued",
                or_(
                    m.TradingCommand.action != "target",
                    cast(m.TradingCommand.payload["expires_at"].astext, DateTime(timezone=True))
                    <= func.clock_timestamp(),
                    ~pending,
                ),
            )
            if controls_only:
                statement = statement.where(m.TradingCommand.action.in_(("pause", "cancel", "reconcile")))
            instance = session.scalar(
                statement.order_by(
                    case(
                        (m.TradingCommand.action == "pause", 0),
                        (m.TradingCommand.action == "cancel", 1),
                        else_=2,
                    ),
                    m.TradingCommand.created_at,
                    m.TradingCommand.id,
                ).limit(1)
            )
            command = row_dict(instance) if instance else None
        if command is None:
            return False
        identity, action = command["id"], command["action"]
        target = TargetCommand.model_validate(command["payload"]) if action == "target" else None
        spec = OrderCommand.model_validate(command["payload"]) if action == "order" else None
        asset, market = {}, {}
        if target or spec:
            if target:
                symbol = target.symbol
            else:
                assert spec is not None
                symbol = spec.order.symbol
            asset, market = self.broker.asset(symbol), self.market_input(symbol)
        cancel_id = None
        with database_session(self.engine) as session:
            account = locked_account(session, self.account_id, self.token)
            current = session.scalar(
                select(m.TradingCommand)
                .where(m.TradingCommand.id == identity, m.TradingCommand.state == "queued")
                .with_for_update()
            )
            if current is None:
                return True
            if target or spec:
                sizing = None
                if target:
                    pending_id = session.scalar(
                        select(m.OrderIntent.id)
                        .where(
                            m.OrderIntent.account_id == self.account_id,
                            m.OrderIntent.strategy_id == target.strategy_id,
                            m.OrderIntent.payload["symbol"].astext == target.symbol,
                            m.OrderIntent.reserved_notional > 0,
                        )
                        .limit(1)
                    )
                    if pending_id and target.expires_at > account["now"]:
                        return False
                    spec, sizing, reason = self.resolve_target(session, account, target)
                    if reason:
                        satisfied = reason == "target_already_satisfied"
                        if satisfied:
                            self.save_target(session, target, sizing)
                        complete_command(
                            session, identity, {"reason": reason, "sizing": sizing}, not satisfied
                        )
                        return True
                assert spec is not None
                result = admit(
                    session,
                    self.account_id,
                    self.token,
                    identity,
                    spec,
                    asset or {},
                    market,
                    command["created_at"],
                    target_sizing=sizing,
                )
                if target and result["state"] == "admitted":
                    self.save_target(session, target, sizing)
                complete_command(session, identity, to_jsonable_python(result), result["state"] == "rejected")
            elif action == "cancel":
                cancel = CancelCommand.model_validate(command["payload"])
                known = session.get(m.BrokerOrder, cancel.broker_order_id)
                if known is None or known.account_id != self.account_id:
                    complete_command(session, identity, {"reason": "unknown_order"}, True)
                    return True
                cancel_id = cancel.broker_order_id
                audit(
                    session, self.account_id, "cancel_requested", {"broker_order_id": str(cancel_id)}, identity
                )
            elif action in ("pause", "reconcile"):
                values: dict = dict(paused=True, armed_until=None)
                if action == "reconcile":
                    values.update(reconciled=False, reason="operator_full_reconciliation")
                    session.execute(
                        update(m.BrokerSyncCheckpoint)
                        .where(m.BrokerSyncCheckpoint.account_id == self.account_id)
                        .values(
                            activity_cursor=None,
                            activity_after=None,
                            order_cursor=None,
                            initial_activities_complete=False,
                            initial_orders_complete=False,
                        )
                    )
                session.execute(
                    update(m.TradingAccount)
                    .where(m.TradingAccount.account_id == self.account_id)
                    .values(**values)
                )
                complete_command(
                    session, identity, {"paused": True, "full_reconciliation_requested": action == "reconcile"}
                )
            elif action == "arm":
                arm = ArmCommand.model_validate(command["payload"])
                reason = None
                if not self.settings.submission_enabled:
                    reason = "runtime_submission_disabled"
                elif (
                    arm.expires_at <= account["now"]
                    or not account["reconciled"]
                    or not account["stream_connected"]
                    or not account["reconciled_at"]
                    or (account["now"] - account["reconciled_at"]).total_seconds()
                    > arm.policy.account_fresh_seconds
                ):
                    reason = "account_not_ready_or_arm_expired"
                if reason:
                    complete_command(session, identity, {"reason": reason}, True)
                    return True
                session.execute(
                    update(m.TradingAccount)
                    .where(m.TradingAccount.account_id == self.account_id)
                    .values(
                        paused=False,
                        run_id=arm.run_id,
                        armed_until=arm.expires_at,
                        policy=arm.policy.model_dump(mode="json"),
                    )
                )
                complete_command(session, identity, {"armed": True, "run_id": str(arm.run_id)})
            if action not in ("order", "target", "cancel"):
                audit(session, self.account_id, "operator_" + action, {"command_id": str(identity)}, identity)
        if cancel_id:
            accepted = self.broker.cancel_once(str(cancel_id))
            with database_session(self.engine) as session:
                locked_account(session, self.account_id, self.token)
                complete_command(
                    session,
                    identity,
                    {
                        "cancel_requested": accepted,
                        "reason": "broker_confirmation_required" if accepted else "cancel_not_accepted",
                    },
                    not accepted,
                )
        return True

    def resolve_target(
        self, session: Session, account: dict, target: TargetCommand
    ) -> tuple[OrderCommand | None, dict, str | None]:
        assert self.account_id
        if not target.signal_id or not target.activation_at or target.inputs:
            return None, {}, "normalized_target_signal_required"
        if target.expires_at <= account["now"]:
            return None, {}, "decision_expired"
        reason, signal = signal_context(
            session,
            self.account_id,
            target.strategy_id,
            target.run_id,
            target.signal_id,
            target.symbol,
            target.decision_at,
            account["snapshot"].get("feed"),
        )
        if reason:
            return None, {}, reason
        if (
            target.activation_at != signal["activation_at"]
            or target.weight != Decimal(signal["weight"])
            or target.price != Decimal(signal["price"])
            or target.rebalance_policy != signal["rebalance_policy"]
        ):
            return None, {}, "target_signal_mismatch"
        if target.decision_at > account["now"] + timedelta(seconds=1):
            return None, {}, "future_decision"
        if (
            account["paused"]
            or not account["armed_until"]
            or account["armed_until"] <= account["now"]
            or str(account["run_id"]) != str(target.run_id)
            or not account["reconciled"]
            or not account["stream_connected"]
        ):
            return None, {}, "account_not_ready_for_target"
        policy = RiskPolicy.model_validate(account["policy"])
        if (
            not account["reconciled_at"]
            or (account["now"] - account["reconciled_at"]).total_seconds() > policy.account_fresh_seconds
        ):
            return None, {}, "account_stale"
        equity = Decimal(account["snapshot"]["equity"])
        previous = session.get(m.StrategyTargetState, (self.account_id, target.strategy_id, target.symbol))
        if previous and previous.activation_at != target.activation_at:
            previous = None
        reused = bool(
            previous and previous.weight == target.weight and target.rebalance_policy == "on_weight_change"
        )
        desired = Decimal(
            target_quantity(
                target.weight,
                equity,
                target.price,
                previous.weight if previous else None,
                previous.quantity if previous else None,
                target.rebalance_policy,
            )
        )
        sizing = dict(
            weight=str(target.weight),
            quantity=str(desired),
            reused=reused,
            sizing_signal_id=str(previous.sizing_signal_id) if reused and previous else str(target.signal_id),
            sizing_decision_id=str(previous.sizing_decision_id)
            if reused and previous
            else str(target.decision_id),
            sizing_price=str(previous.sizing_price) if reused and previous else str(target.price),
            sizing_equity=str(previous.sizing_equity) if reused and previous else str(equity),
            rebalance_policy=target.rebalance_policy,
        )
        attributed = account["attribution"].get(str(target.strategy_id), {}).get("positions", {})
        held = Decimal(attributed.get(target.symbol, {}).get("quantity", "0"))
        delta = -held if held * desired < 0 else desired - held
        if not delta:
            return None, sizing, "target_already_satisfied"
        spec = OrderCommand.model_validate(
            {
                **target.model_dump(
                    mode="json", exclude={"symbol", "weight", "price", "activation_at", "rebalance_policy"}
                ),
                "order": {
                    "symbol": target.symbol,
                    "side": "buy" if delta > 0 else "sell",
                    "qty": str(abs(delta)),
                },
            }
        )
        return spec, sizing, None

    def save_target(self, session: Session, target: TargetCommand, sizing: dict | None) -> None:
        assert self.account_id
        if not sizing or sizing["reused"]:
            return
        statement = insert(m.StrategyTargetState).values(
            account_id=self.account_id,
            strategy_id=target.strategy_id,
            symbol=target.symbol,
            activation_at=target.activation_at,
            weight=target.weight,
            quantity=Decimal(sizing["quantity"]),
            sizing_signal_id=target.signal_id,
            sizing_decision_id=target.decision_id,
            sizing_price=target.price,
            sizing_equity=Decimal(sizing["sizing_equity"]),
        )
        session.execute(
            statement.on_conflict_do_update(
                index_elements=[
                    m.StrategyTargetState.account_id,
                    m.StrategyTargetState.strategy_id,
                    m.StrategyTargetState.symbol,
                ],
                set_={
                    name: getattr(statement.excluded, name)
                    for name in (
                        "activation_at",
                        "weight",
                        "quantity",
                        "sizing_signal_id",
                        "sizing_decision_id",
                        "sizing_price",
                        "sizing_equity",
                    )
                },
            )
        )
        audit(
            session,
            self.account_id,
            "target_sized",
            {"strategy_id": str(target.strategy_id), "symbol": target.symbol, **sizing},
            uuid5(target.decision_id, "sizing"),
        )

    def dispatch_once(self) -> bool:
        assert self.account_id
        if not self.settings.submission_enabled:
            return False
        with database_session(self.engine) as session:
            locked_account(session, self.account_id, self.token)
            row = session.scalar(
                select(m.OrderIntent)
                .where(m.OrderIntent.account_id == self.account_id, m.OrderIntent.state == "admitted")
                .order_by(m.OrderIntent.created_at, m.OrderIntent.id)
                .limit(1)
            )
            candidate = row_dict(row) if row else None
        if candidate is None:
            return False
        asset, market = (
            self.broker.asset(candidate["payload"]["symbol"]),
            self.market_input(candidate["payload"]["symbol"]),
        )
        with database_session(self.engine) as session:
            account = locked_account(session, self.account_id, self.token)
            if (
                account["paused"]
                or not account["reconciled"]
                or not account["stream_connected"]
                or not account["armed_until"]
                or account["armed_until"] <= account["now"]
            ):
                return False
            instance = session.scalar(
                select(m.OrderIntent)
                .where(m.OrderIntent.id == candidate["id"], m.OrderIntent.state == "admitted")
                .with_for_update()
            )
            if instance is None:
                return False
            intent = row_dict(instance)
            reason = None
            if intent["strategy_id"]:
                strategy = session.get(m.StrategyRun, intent["strategy_id"])
                command = session.get(m.TradingCommand, intent["id"])
                if strategy is None or (
                    command and command.action == "target" and strategy.state != "running"
                ):
                    reason = "strategy_not_running"
                elif intent["signal_id"]:
                    reason, _ = signal_context(
                        session,
                        self.account_id,
                        intent["strategy_id"],
                        intent["run_id"],
                        intent["signal_id"],
                        intent["payload"]["symbol"],
                        intent["decision_at"],
                        account["snapshot"].get("feed"),
                    )
                elif command and command.action == "target":
                    reason = "normalized_target_signal_required"
            if intent["expires_at"] <= account["now"] or str(intent["run_id"]) != str(account["run_id"]):
                reason = "decision_expired"
            if reason:
                session.execute(
                    update(m.OrderIntent)
                    .where(m.OrderIntent.id == intent["id"])
                    .values(state="expired", reserved_notional=0, reason=reason)
                )
                audit(
                    session,
                    self.account_id,
                    "target_expired",
                    {"intent_id": str(intent["id"]), "reason": reason},
                )
                return True
            policy = RiskPolicy.model_validate(account["policy"])
            if (
                not account["reconciled_at"]
                or (account["now"] - account["reconciled_at"]).total_seconds() > policy.account_fresh_seconds
            ):
                return False
            if intent["dispatch_count"] != 0:
                raise RuntimeError("Order attempt invariant violated")
            payload = {k: v for k, v in intent["payload"].items() if k != "client_order_id"}
            command = OrderCommand.model_validate({"run_id": str(intent["run_id"]), "order": payload})
            reservations = [
                dict(
                    symbol=r.payload["symbol"],
                    notional=str(r.reserved_notional),
                    quantity=r.payload.get("qty"),
                    side=r.payload["side"],
                    order_class=r.payload.get("order_class"),
                )
                for r in session.scalars(
                    select(m.OrderIntent).where(
                        m.OrderIntent.account_id == self.account_id,
                        m.OrderIntent.reserved_notional > 0,
                        m.OrderIntent.id != intent["id"],
                    )
                )
            ]
            reason, reservation, inputs = evaluate(
                policy, account, command, asset or {}, market, reservations, account["now"]
            )
            audit(
                session,
                self.account_id,
                "dispatch_risk_recheck",
                {
                    "intent_id": str(intent["id"]),
                    "reason": reason or "approved",
                    "reservation": str(reservation),
                    "policy": policy.model_dump(mode="json"),
                    "inputs": to_jsonable_python(inputs),
                },
            )
            if reason:
                session.execute(
                    update(m.OrderIntent)
                    .where(m.OrderIntent.id == intent["id"])
                    .values(state="rejected", reserved_notional=0, reason="dispatch_" + reason)
                )
                return True
            session.execute(
                update(m.OrderIntent)
                .where(m.OrderIntent.id == intent["id"])
                .values(
                    state="dispatching",
                    dispatch_count=1,
                    reserved_notional=reservation,
                    dispatched_at=func.clock_timestamp(),
                    updated_at=func.clock_timestamp(),
                )
            )
            audit(
                session,
                self.account_id,
                "dispatch_started",
                {"intent_id": str(intent["id"]), "client_order_id": intent["client_order_id"]},
                uuid5(intent["id"], "dispatch"),
            )
        transport = self.transport_factory(self.settings)
        try:
            response = transport.submit_once(intent["payload"])
        except BrokerRejected as error:
            with database_session(self.engine) as session:
                locked_account(session, self.account_id, self.token)
                session.execute(
                    update(m.OrderIntent)
                    .where(m.OrderIntent.id == intent["id"])
                    .values(state="rejected", reserved_notional=0, reason=type(error).__name__)
                )
                audit(session, self.account_id, "broker_rejected", {"intent_id": str(intent["id"])})
        except AmbiguousSubmission:
            with database_session(self.engine) as session:
                locked_account(session, self.account_id, self.token)
                session.execute(
                    update(m.OrderIntent)
                    .where(m.OrderIntent.id == intent["id"])
                    .values(state="unresolved", reason="ambiguous_submission")
                )
                session.execute(
                    update(m.TradingAccount)
                    .where(m.TradingAccount.account_id == self.account_id)
                    .values(reconciled=False, reason="unresolved_submission")
                )
                audit(session, self.account_id, "submission_unresolved", {"intent_id": str(intent["id"])})
        else:
            with database_session(self.engine) as session:
                locked_account(session, self.account_id, self.token)
                store_order(session, self.account_id, response)
                session.execute(
                    update(m.OrderIntent)
                    .where(m.OrderIntent.id == intent["id"])
                    .values(state="accepted", broker_order_id=UUID(response["id"]))
                )
                audit(
                    session,
                    self.account_id,
                    "broker_acknowledged",
                    {"intent_id": str(intent["id"]), "broker_order_id": response["id"]},
                )
        finally:
            transport.close()
        return True


async def run_account(settings: PaperSettings, journal: Journal, stopping) -> None:
    process_id = uuid4()
    if not settings.credentials_present():
        while not stopping():
            await asyncio.to_thread(
                journal.heartbeat,
                "execution",
                process_id,
                "disabled",
                {"reason": "credentials_missing", "submission_enabled": False},
            )
            await asyncio.sleep(settings.poll_seconds)
        return
    worker = AccountWorker(settings, journal)
    await asyncio.to_thread(worker.initialize)
    account_id = worker.account_id
    assert account_id

    async def receive() -> None:
        while not stopping():
            try:
                async for message in trading_updates(settings):
                    if message["kind"] == "connected":
                        await asyncio.to_thread(worker.block, "stream_reconnect_reconciliation", True)
                    else:
                        raw = message["data"]
                        await asyncio.to_thread(save_event, journal, account_id, raw)

                        def apply_update(event: dict) -> None:
                            with database_session(worker.engine) as session:
                                locked_account(session, account_id, worker.token)
                                store_order(session, account_id, event["order"])
                                session.execute(
                                    update(m.TradingAccount)
                                    .where(m.TradingAccount.account_id == account_id)
                                    .values(
                                        reconciled=False,
                                        reason="stream_update_pending",
                                        stream_connected=True,
                                        updated_at=func.clock_timestamp(),
                                    )
                                )

                        await asyncio.to_thread(apply_update, raw)
                await asyncio.to_thread(worker.block, "trading_stream_disconnected", False)
            except (BrokerUnavailable, ConnectionClosed, OSError, TimeoutError, ValueError) as error:
                await asyncio.to_thread(worker.block, "trading_stream_disconnected", False)
                log.warning("paper_stream_retry", extra={"error_type": type(error).__name__})
            await asyncio.sleep(3)

    stream = asyncio.create_task(receive())
    try:
        while not stopping():
            if stream.done():
                stream.result()
            try:
                for _ in range(settings.command_batch):
                    if not await asyncio.to_thread(worker.process_command, True):
                        break
                await asyncio.to_thread(worker.reconcile)
                for _ in range(settings.command_batch):
                    if not await asyncio.to_thread(worker.process_command):
                        break
                await asyncio.to_thread(worker.dispatch_once)
                with database_session(journal.engine) as session:
                    state = session.get(m.TradingAccount, account_id)
                    assert state
                    details = dict(
                        reason=state.reason,
                        stream_connected=state.stream_connected,
                        submission_enabled=settings.submission_enabled
                        and not state.paused
                        and bool(state.armed_until and state.armed_until > datetime.now(UTC)),
                    )
                    status = "ready" if state.reconciled else "blocked"
                await asyncio.to_thread(journal.heartbeat, "execution", process_id, status, details)
            except (BrokerUnavailable, ValueError, KeyError, ArithmeticError) as error:
                await asyncio.to_thread(worker.block, "broker_or_command_evidence_invalid")
                await asyncio.to_thread(
                    journal.heartbeat,
                    "execution",
                    process_id,
                    "blocked",
                    {"reason": type(error).__name__, "submission_enabled": False},
                )
                log.warning("account_cycle_blocked", extra={"error_type": type(error).__name__})
            await asyncio.sleep(settings.poll_seconds)
    finally:
        stream.cancel()
        await asyncio.gather(stream, return_exceptions=True)
        worker.broker.close()
