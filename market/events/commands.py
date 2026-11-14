"""Append durable control/audit events within the owning ORM transaction."""

from uuid import UUID

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from market.models import EventJournal


def append_command(session: Session, identity: UUID, topic: str, partition: str, body: dict) -> None:
    session.execute(
        insert(EventJournal)
        .values(event_id=identity, topic=topic, partition_key=partition, body=body)
        .on_conflict_do_nothing(index_elements=[EventJournal.event_id])
    )
