"""Independent Redis -> ORM persistence. ACK follows the committed transaction."""

import logging
from collections import defaultdict
from datetime import UTC, datetime

from sqlalchemy.dialects.postgresql import insert

from market import models as m
from market.data.domain import Instrument, Observation, Origin
from market.data.storage import PostgresStore
from market.database import database_session
from market.events.bus import DECISION_GROUP, PERSISTENCE_GROUP, MarketBus
from market.events.contracts import MarketEvent

log = logging.getLogger(__name__)


def persist_once(
    bus: MarketBus,
    store: PostgresStore,
    owner: str,
    *,
    limit: int = 500,
    before_ack=None,
    database_budget_mb: int = 100000,
) -> int:
    bus.group(PERSISTENCE_GROUP)
    deliveries = bus.deliveries(PERSISTENCE_GROUP, owner, count=limit)
    if not deliveries:
        return 0
    if store.database_bytes() >= database_budget_mb * 1000000:
        raise RuntimeError("Market persistence reached the configured database budget")
    grouped = defaultdict(list)
    for stream_id, fields in deliveries:
        event = MarketEvent.model_validate_json(fields.get("event"))
        if event.available_at > datetime.now(UTC):
            raise ValueError("Market event is not yet available")
        grouped[(event.provider, event.feed)].append((stream_id, event))
    ids = []
    for (provider, feed), entries in grouped.items():
        instruments = {
            event.provider_instrument_id: Instrument(
                event.provider_instrument_id, event.provider_symbol, event.provider_symbol
            )
            for _, event in entries
        }
        # This is the database component, not the collector's hot path.
        series = store.resolve_series(list(instruments.values()), feed, provider)
        observations = [
            Observation(
                event.payload.bar(event.provider_symbol),
                event.payload.session,
                Origin(event.origin),
                event.observed_at,
                event.received_at,
            )
            for _, event in entries
        ]
        with database_session(store.engine) as session:
            store._write(
                session, series, observations, [event.model_dump(mode="json") for _, event in entries]
            )
            session.execute(
                insert(m.MarketStreamReceipt)
                .values(
                    [
                        dict(stream=bus.settings.market_stream, stream_id=stream_id, event_id=event.event_id)
                        for stream_id, event in entries
                    ]
                )
                .on_conflict_do_nothing()
            )
        for stream_id, event in entries:
            ids.append(stream_id)
            log.info(
                "market_event_persisted",
                extra={
                    "event_id": str(event.event_id),
                    "stream_id": stream_id,
                    "provider": provider,
                    "feed": feed,
                },
            )
    if before_ack:
        before_ack()
    bus.redis.xack(bus.settings.market_stream, PERSISTENCE_GROUP, *ids)
    bus.safe_trim()
    return len(ids)


def persist_decisions_once(bus: MarketBus, store: PostgresStore, owner: str, *, limit: int = 100) -> int:
    from market.strategies.worker import persist_emission

    bus.group(DECISION_GROUP, stream=bus.settings.decision_stream)
    rows = bus.deliveries(DECISION_GROUP, owner, stream=bus.settings.decision_stream, count=limit)
    completed = []
    for stream_id, fields in rows:
        with database_session(store.engine) as session:
            if not persist_emission(session, fields["signal"]):
                # Keep this delivery pending. Re-read it on the next owner cycle,
                # rather than wait for the orphan-reclaim timeout.
                break
        completed.append(stream_id)
    if completed:
        bus.redis.xack(bus.settings.decision_stream, DECISION_GROUP, *completed)
        bus.safe_trim(stream=bus.settings.decision_stream)
    return len(completed)
