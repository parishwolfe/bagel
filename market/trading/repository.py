"""ORM commands and financial audit; callers own transaction boundaries."""

from datetime import timedelta
from uuid import UUID, uuid4, uuid5

from sqlalchemy import Engine, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from market import models as m
from market.database import database_session, row_dict
from market.events.commands import append_command
from market.research.artifacts import digest_json

COMMAND_NAMESPACE = UUID("968e3871-dc71-43ba-93bd-aaea40347622")


class OwnershipLost(RuntimeError):
    pass


class CommandConflict(ValueError):
    pass


def locked_account(session: Session, account_id: UUID, token: UUID | None = None) -> dict:
    row = session.scalar(
        select(m.TradingAccount)
        .where(m.TradingAccount.account_id == account_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise KeyError("Trading account not found")
    if token is not None and row.owner_token != token:
        raise OwnershipLost("Stale account worker")
    return {**row_dict(row), "now": session.scalar(select(func.clock_timestamp()))}


def audit(
    session: Session, account_id: UUID, event_type: str, body: dict, identity: UUID | None = None
) -> None:
    identity = identity or uuid4()
    session.execute(
        insert(m.TradingAudit)
        .values(account_id=account_id, event_id=identity, event_type=event_type, body=body)
        .on_conflict_do_nothing(index_elements=[m.TradingAudit.event_id])
    )
    append_command(session, identity, "audit.trading." + event_type, str(account_id), body)


class TradingRepository:
    def __init__(self, engine: Engine):
        self.engine = engine

    def submit(self, account_id: UUID, action: str, payload: dict, key: UUID | None = None) -> UUID:
        request_hash = digest_json((str(account_id), action, payload))
        identity = key or (
            uuid4() if action in ("pause", "cancel", "reconcile") else uuid5(COMMAND_NAMESPACE, request_hash)
        )
        with database_session(self.engine) as session:
            if session.get(m.TradingAccount, account_id) is None:
                raise KeyError("Trading account not found")
            session.execute(
                insert(m.TradingCommand)
                .values(
                    id=identity,
                    request_hash=request_hash,
                    account_id=account_id,
                    action=action,
                    payload=payload,
                )
                .on_conflict_do_nothing(index_elements=[m.TradingCommand.id])
            )
            row = session.get(m.TradingCommand, identity)
            assert row is not None
            if row.request_hash != request_hash:
                raise CommandConflict("Idempotency key belongs to a different command")
            append_command(
                session,
                identity,
                "command.trading." + action,
                str(account_id),
                {"command_id": str(identity), "account_id": str(account_id)},
            )
        return identity

    def command(self, identity: UUID) -> dict:
        with database_session(self.engine) as session:
            row = session.get(m.TradingCommand, identity)
            if row is None:
                raise KeyError("Trading command not found")
            return row_dict(row)

    def accounts(self) -> list[dict]:
        with database_session(self.engine) as session:
            now = session.scalar(select(func.clock_timestamp()))
            assert now is not None
            result = []
            for instance in session.scalars(select(m.TradingAccount).order_by(m.TradingAccount.account_id)):
                row = row_dict(instance)
                enabled = row["snapshot"].get("runtime_submission_enabled", False)
                armed = bool(
                    enabled
                    and not row["paused"]
                    and row["armed_until"]
                    and row["armed_until"] > now
                    and row["reconciled"]
                    and row["stream_connected"]
                    and row["reconciled_at"]
                    and row["reconciled_at"] > now - timedelta(seconds=row["policy"]["account_fresh_seconds"])
                )
                result.append({**row, "runtime_submission_enabled": enabled, "submission_armed": armed})
            return result

    def orders(self, account_id: UUID, offset: int = 0, limit: int = 50) -> list[dict]:
        with database_session(self.engine) as session:
            return [
                row_dict(r)
                for r in session.scalars(
                    select(m.OrderIntent)
                    .where(m.OrderIntent.account_id == account_id)
                    .order_by(m.OrderIntent.created_at.desc(), m.OrderIntent.id)
                    .offset(offset)
                    .limit(limit)
                )
            ]

    def risk(self, account_id: UUID, offset: int = 0, limit: int = 50) -> list[dict]:
        with database_session(self.engine) as session:
            return [
                row_dict(r)
                for r in session.scalars(
                    select(m.RiskDecision)
                    .join(m.OrderIntent, m.OrderIntent.id == m.RiskDecision.intent_id)
                    .where(m.OrderIntent.account_id == account_id)
                    .order_by(m.RiskDecision.created_at.desc(), m.RiskDecision.intent_id)
                    .offset(offset)
                    .limit(limit)
                )
            ]

    def audit(self, account_id: UUID, after: int = 0, limit: int = 50) -> list[dict]:
        with database_session(self.engine) as session:
            return [
                row_dict(r)
                for r in session.scalars(
                    select(m.TradingAudit)
                    .where(m.TradingAudit.account_id == account_id, m.TradingAudit.id > after)
                    .order_by(m.TradingAudit.id)
                    .limit(limit)
                )
            ]


def complete_command(session: Session, identity: UUID, result: dict, rejected: bool = False) -> None:
    session.execute(
        update(m.TradingCommand)
        .where(m.TradingCommand.id == identity, m.TradingCommand.state == "queued")
        .values(
            state="rejected" if rejected else "complete", result=result, completed_at=func.clock_timestamp()
        )
    )
