"""Account-owning read/reconciliation evidence process. Never submits orders.

This read model deliberately remains unreconciled until the durable accounting
and admission slice proves equality. Broker snapshots are not a trading ledger.
"""

import asyncio
import logging
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from websockets.exceptions import ConnectionClosed

from market import models as m
from market.database import database_session
from market.events.journal import Journal
from market.execution.alpaca import BrokerUnavailable, PaperREST, PaperSettings, trading_updates
from market.research.artifacts import digest_json

log = logging.getLogger(__name__)
ORDER_FIELDS = (
    "id",
    "client_order_id",
    "symbol",
    "side",
    "qty",
    "filled_qty",
    "filled_avg_price",
    "type",
    "time_in_force",
    "status",
    "submitted_at",
    "updated_at",
)
POSITION_FIELDS = ("asset_id", "symbol", "qty", "avg_entry_price", "market_value", "unrealized_pl")


def save_event(journal: Journal, account_id: UUID, event: dict) -> None:
    identity = {
        "order": str(event["order"]["id"]),
        "event": event["event"],
        "time": str(event.get("timestamp")),
        "execution": event.get("execution_id"),
    }
    stamp = event.get("timestamp")
    with database_session(journal.engine) as session:
        session.execute(
            insert(m.PaperStreamEvidence)
            .values(
                event_id=digest_json(identity),
                account_id=account_id,
                broker_order_id=UUID(event["order"]["id"]),
                event_type=event["event"],
                broker_timestamp=datetime.fromisoformat(stamp) if isinstance(stamp, str) else stamp,
            )
            .on_conflict_do_nothing()
        )


def snapshot(broker: PaperREST, journal: Journal) -> UUID:
    account = broker.get("/v2/account")
    positions = broker.get("/v2/positions")
    orders = broker.get("/v2/orders", {"status": "all", "limit": 500, "direction": "desc"})
    if not isinstance(account, dict) or not isinstance(positions, list) or not isinstance(orders, list):
        raise BrokerUnavailable("Invalid Paper account snapshot")
    identity = UUID(account["id"])
    with database_session(journal.engine) as session:
        statement = insert(m.PaperAccount).values(
            account_id=identity,
            status=account["status"],
            cash=Decimal(account["cash"]),
            equity=Decimal(account["equity"]),
            buying_power=Decimal(account["buying_power"]),
            trading_blocked=bool(account["trading_blocked"]),
            positions=[{k: p.get(k) for k in POSITION_FIELDS} for p in positions],
            orders=[{k: o.get(k) for k in ORDER_FIELDS} for o in orders],
            snapshot_at=func.clock_timestamp(),
        )
        session.execute(
            statement.on_conflict_do_update(
                index_elements=[m.PaperAccount.account_id],
                set_={
                    **{
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
                        )
                    },
                    "reconciled": False,
                    "reconciliation_reason": "ledger_not_reconciled",
                },
            )
        )
    for page in broker.fill_pages():
        if page:
            with database_session(journal.engine) as session:
                session.execute(
                    insert(m.PaperFillEvidence)
                    .values(
                        [
                            dict(
                                account_id=identity,
                                activity_id=f["id"],
                                broker_order_id=UUID(f["order_id"]),
                                symbol=f["symbol"],
                                side=f["side"],
                                quantity=Decimal(f["qty"]),
                                price=Decimal(f["price"]),
                                transaction_at=datetime.fromisoformat(f["transaction_time"]),
                            )
                            for f in page
                        ]
                    )
                    .on_conflict_do_nothing()
                )
    return identity


async def monitor(settings: PaperSettings, journal: Journal, stopping) -> None:
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
            await asyncio.sleep(2)
        return
    broker = PaperREST(settings)
    connected = False
    account_id: UUID | None = None

    async def receive() -> None:
        nonlocal connected
        while not stopping():
            try:
                async for message in trading_updates(settings):
                    if message["kind"] == "connected":
                        connected = True
                        continue
                    event = message["data"]
                    if account_id is None:
                        raise BrokerUnavailable("Trading update arrived before account identity")
                    await asyncio.to_thread(save_event, journal, account_id, event)
                connected = False
                await asyncio.sleep(5)
            except (BrokerUnavailable, ConnectionClosed, OSError, TimeoutError, ValueError) as error:
                connected = False
                log.warning("paper_stream_retry", extra={"error_type": type(error).__name__})
                await asyncio.sleep(5)

    task = None
    try:
        account_id = await asyncio.to_thread(snapshot, broker, journal)
        task = asyncio.create_task(receive())
        while not stopping():
            try:
                account_id = await asyncio.to_thread(snapshot, broker, journal)
                await asyncio.to_thread(
                    journal.heartbeat,
                    "execution",
                    process_id,
                    "blocked",
                    {
                        "reason": "ledger_not_reconciled",
                        "stream_connected": connected,
                        "submission_enabled": False,
                        "snapshot_at": datetime.now(UTC).isoformat(),
                    },
                )
            except BrokerUnavailable as error:
                await asyncio.to_thread(
                    journal.heartbeat,
                    "execution",
                    process_id,
                    "blocked",
                    {"reason": type(error).__name__, "submission_enabled": False},
                )
            await asyncio.sleep(10)
    finally:
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        broker.close()
