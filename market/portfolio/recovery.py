"""Bounded ORM replay of canonical activities; original ledger records remain immutable."""

import hashlib
from decimal import Decimal
from uuid import UUID, uuid5

from sqlalchemy import Uuid, case, cast, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from market import models as m
from market.portfolio.accounting import AccountLedger
from market.research.artifacts import canonical_json, digest_json
from market.trading.repository import audit


def apply_attribution(attribution: dict, key: str, activity: dict) -> None:
    previous = attribution.get(key, {})
    ledger = AccountLedger.restore(
        Decimal(previous.get("cash", "0")),
        previous.get("positions", {}),
        Decimal(previous.get("realized", "0")),
        Decimal(previous.get("fees", "0")),
    )
    ledger.apply(activity)
    attribution[key] = {
        "cash": str(ledger.cash),
        "positions": ledger.dump(),
        "realized": str(ledger.realized),
        "fees": str(ledger.fees),
    }


def rebuild(session: Session, account: dict, account_id: UUID) -> None:
    if account["opening_cash"] is None:
        raise ValueError("A confirmed opening balance is required")
    ledger = AccountLedger(account["opening_cash"])
    attribution: dict = {}
    source = hashlib.sha256()
    count, last = 0, None
    order_id = case(
        (m.AccountActivity.activity_type == "FILL", cast(m.AccountActivity.payload["order_id"].astext, Uuid)),
        else_=None,
    )
    statement = (
        select(m.AccountActivity, m.OrderIntent.strategy_id, m.BrokerOrder.root_intent)
        .outerjoin(m.BrokerOrder, m.BrokerOrder.id == order_id)
        .outerjoin(m.OrderIntent, m.OrderIntent.id == m.BrokerOrder.root_intent)
        .where(m.AccountActivity.account_id == account_id)
        .order_by(m.AccountActivity.effective_at, m.AccountActivity.activity_id)
        .execution_options(yield_per=500)
    )
    with session.execute(statement) as rows:
        for entry, strategy_id, root_intent in rows:
            symbol, quantity, price, cash_delta, realized = ledger.apply(entry.payload)
            key = str(strategy_id) if strategy_id else "operator" if root_intent else "external"
            apply_attribution(attribution, key, entry.payload)
            session.execute(
                insert(m.LedgerEntry)
                .values(
                    account_id=account_id,
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
            source.update(canonical_json((entry.payload_sha256, root_intent, strategy_id)))
            count += 1
            last = (entry.effective_at, entry.activity_id)
    totals = session.execute(
        select(
            func.coalesce(func.sum(m.LedgerEntry.cash_delta), 0),
            func.coalesce(func.sum(m.LedgerEntry.realized_pnl), 0),
        ).where(m.LedgerEntry.account_id == account_id)
    ).one()
    identity = "rebuild:" + source.hexdigest()
    session.execute(
        insert(m.LedgerEntry)
        .values(
            account_id=account_id,
            activity_id=identity,
            attribution="reconciliation",
            quantity=0,
            price=0,
            cash_delta=ledger.cash - account["opening_cash"] - totals[0],
            realized_pnl=ledger.realized - totals[1],
            effective_at=func.clock_timestamp(),
        )
        .on_conflict_do_nothing()
    )
    session.execute(
        update(m.TradingAccount)
        .where(m.TradingAccount.account_id == account_id)
        .values(
            cash=ledger.cash,
            positions=ledger.dump(),
            attribution=attribution,
            realized_pnl=ledger.realized,
            fees=ledger.fees,
            last_activity_time=last[0] if last else None,
            last_activity_id=last[1] if last else None,
            rebuild_required=False,
        )
    )
    session.execute(
        update(m.AccountActivity)
        .where(m.AccountActivity.account_id == account_id, ~m.AccountActivity.applied)
        .values(applied=True)
    )
    audit(
        session,
        account_id,
        "portfolio_rebuilt",
        {
            "source_sha256": source.hexdigest(),
            "source_activities": count,
            "cash": str(ledger.cash),
            "positions": ledger.dump(),
            "realized": str(ledger.realized),
            "attribution_sha256": digest_json(attribution),
            "attribution": attribution,
        },
        uuid5(account_id, identity),
    )
