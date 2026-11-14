"""ORM archive and durable control/audit outbox. Live bars enter through Redis."""

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import cast
from uuid import UUID

from redis import Redis
from sqlalchemy import Engine, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from market import models as m
from market.data.domain import Observation
from market.database import database_session, row_dict
from market.events.contracts import event_from_observation

STREAM = "bagel:committed:v1"


def archive(
    session: Session,
    series: dict[str, int],
    observations: Sequence[Observation],
    *,
    envelopes: list[dict] | None = None,
) -> list[UUID]:
    if not observations:
        return []
    metadata = {
        row.id: row
        for row in session.execute(
            select(m.Series.id, m.Series.instrument_id, m.Source.code, m.Series.feed)
            .join(m.Source, m.Source.id == m.Series.source_id)
            .where(m.Series.id.in_(set(series[o.bar.symbol] for o in observations)))
        )
    }
    events = (
        envelopes
        if envelopes is not None
        else [
            event_from_observation(
                o,
                series_id=series[o.bar.symbol],
                instrument_id=metadata[series[o.bar.symbol]].instrument_id,
                source=metadata[series[o.bar.symbol]].code,
                feed=metadata[series[o.bar.symbol]].feed,
            ).model_dump(mode="json")
            for o in observations
        ]
    )
    identities = [UUID(e["event_id"]) for e in events]
    unique = {}
    universe = {}
    for observation, event, identity in zip(observations, events, identities, strict=True):
        sid = series[observation.bar.symbol]
        unique[(sid, observation.bar.timestamp, identity)] = dict(
            series_id=sid,
            bar_start=observation.bar.timestamp,
            event_id=identity,
            available_at=datetime.fromisoformat(event["available_at"].replace("Z", "+00:00")),
            observed_at=observation.observed_at,
            envelope=event,
        )
        if sid not in universe:
            universe[sid] = [observation.received_at, observation.received_at]
        else:
            universe[sid][0] = min(universe[sid][0], observation.received_at)
            universe[sid][1] = max(universe[sid][1], observation.received_at)
    rows = list(unique.values())
    for begin in range(0, len(rows), 500):
        session.execute(insert(m.BarRevision).values(rows[begin : begin + 500]).on_conflict_do_nothing())
    statement = insert(m.IngestedUniverse).values(
        [
            dict(series_id=sid, first_received_at=times[0], last_received_at=times[1])
            for sid, times in universe.items()
        ]
    )
    session.execute(
        statement.on_conflict_do_update(
            index_elements=[m.IngestedUniverse.series_id],
            set_={
                "last_received_at": func.greatest(
                    m.IngestedUniverse.last_received_at, statement.excluded.last_received_at
                )
            },
        )
    )
    return identities


class Journal:
    def __init__(self, engine: Engine, stream: str = STREAM):
        self.engine, self.stream = engine, stream

    def sequence(self, limit: int = 1000) -> int:
        with database_session(self.engine) as session:
            session.scalar(select(func.pg_advisory_xact_lock(74021811)))
            counter = session.scalar(select(m.PublicationCounter).with_for_update())
            assert counter is not None
            rows = session.scalars(
                select(m.EventJournal)
                .where(m.EventJournal.sequence.is_(None))
                .order_by(m.EventJournal.created_at, m.EventJournal.event_id)
                .limit(limit)
                .with_for_update()
            ).all()
            for row in rows:
                counter.last_sequence += 1
                row.sequence = counter.last_sequence
            return len(rows)

    def after(self, sequence: int, limit: int = 1000) -> list[dict]:
        with database_session(self.engine) as session:
            return [
                row_dict(r)
                for r in session.scalars(
                    select(m.EventJournal)
                    .where(m.EventJournal.sequence > sequence)
                    .order_by(m.EventJournal.sequence)
                    .limit(limit)
                )
            ]

    def revision(self, body: dict) -> dict:
        with database_session(self.engine) as session:
            start = body["bar_start"]
            row = session.get(
                m.BarRevision,
                (
                    body["series_id"],
                    datetime.fromisoformat(start) if isinstance(start, str) else start,
                    UUID(str(body["revision_id"])),
                ),
            )
            if row is None:
                raise ValueError("Journal revision is unavailable")
            return row.envelope

    def publish(self, redis: Redis, limit: int = 1000, max_length: int = 200_000) -> int:
        self.sequence(limit)
        last = cast(list, redis.xrevrange(self.stream, count=1))
        cursor = int(last[0][0].split("-")[0]) if last else 0
        rows = self.after(cursor, limit)
        for row in rows:
            redis.xadd(
                self.stream,
                {"event_id": str(row["event_id"]), "sequence": str(row["sequence"])},
                id=f"{row['sequence']}-0",
                maxlen=max_length,
                approximate=True,
            )
        delivered = rows[-1]["sequence"] if rows else cursor
        if delivered:
            with database_session(self.engine) as session:
                session.execute(
                    update(m.EventJournal)
                    .where(m.EventJournal.sequence <= delivered, m.EventJournal.published_at.is_(None))
                    .values(published_at=func.clock_timestamp())
                )
        return len(rows)

    def heartbeat(self, worker: str, process_id: UUID, status: str, details: dict) -> None:
        with database_session(self.engine) as session:
            statement = insert(m.WorkerHeartbeat).values(
                worker=worker, process_id=process_id, status=status, details=details
            )
            session.execute(
                statement.on_conflict_do_update(
                    index_elements=[m.WorkerHeartbeat.worker],
                    set_={
                        **{
                            name: getattr(statement.excluded, name)
                            for name in ("process_id", "status", "details")
                        },
                        "updated_at": func.clock_timestamp(),
                    },
                )
            )

    def workers(self) -> list[dict]:
        with database_session(self.engine) as session:
            now = session.scalar(select(func.clock_timestamp()))
            assert now is not None
            rows = [
                {**row_dict(r), "live": r.updated_at > now - timedelta(seconds=30)}
                for r in session.scalars(select(m.WorkerHeartbeat).order_by(m.WorkerHeartbeat.worker))
            ]
        from market.events.bus import MarketBus

        bus = MarketBus.from_settings()
        try:
            current = bus.workers()
            roles = {r["worker"] for r in current}
            return [r for r in rows if r["worker"].split(":")[0] not in roles] + current
        finally:
            bus.close()

    def backlog(self) -> dict:
        with database_session(self.engine) as session:
            unsequenced = (
                session.scalar(
                    select(func.count()).select_from(m.EventJournal).where(m.EventJournal.sequence.is_(None))
                )
                or 0
            )
            pending = (
                session.scalar(
                    select(func.count())
                    .select_from(m.EventJournal)
                    .where(m.EventJournal.published_at.is_(None))
                )
                or 0
            )
            oldest = session.scalar(
                select(func.min(m.EventJournal.created_at)).where(m.EventJournal.published_at.is_(None))
            )
            sequence = session.scalar(select(m.PublicationCounter.last_sequence)) or 0
            return dict(
                unsequenced=unsequenced, unpublished=pending, oldest_pending_at=oldest, last_sequence=sequence
            )


def committed_checkpoint(session: Session, consumer: str, sequence: int) -> None:
    statement = insert(m.ConsumerCheckpoint).values(consumer=consumer, last_sequence=sequence)
    session.execute(
        statement.on_conflict_do_update(
            index_elements=[m.ConsumerCheckpoint.consumer],
            set_={
                "last_sequence": func.greatest(m.ConsumerCheckpoint.last_sequence, sequence),
                "updated_at": func.clock_timestamp(),
            },
        )
    )


def checkpoint(engine: Engine, consumer: str) -> int:
    with database_session(engine) as session:
        return (
            session.scalar(
                select(m.ConsumerCheckpoint.last_sequence).where(m.ConsumerCheckpoint.consumer == consumer)
            )
            or 0
        )
