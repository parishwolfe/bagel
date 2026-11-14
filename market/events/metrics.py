"""Authenticated local control-plane metrics for independently running workers."""

from datetime import UTC, datetime

from prometheus_client import CollectorRegistry, Gauge, generate_latest
from sqlalchemy import Engine, func, select

from market import models as m
from market.database import database_session
from market.events.journal import Journal


def platform_metrics(engine: Engine) -> bytes:
    registry = CollectorRegistry()
    journal = Journal(engine)
    backlog = journal.backlog()
    for field in ("unsequenced", "unpublished", "last_sequence"):
        Gauge("bagel_journal_" + field, "Durable journal " + field, registry=registry).set(backlog[field])
    oldest = backlog["oldest_pending_at"]
    Gauge("bagel_journal_oldest_pending_seconds", "Age of oldest unpublished event", registry=registry).set(
        max(0, (datetime.now(UTC) - oldest).total_seconds()) if oldest else 0
    )
    live = Gauge(
        "bagel_worker_live", "At least one live process for a worker role", ["role"], registry=registry
    )
    ready = Gauge(
        "bagel_worker_ready", "At least one ready process for a worker role", ["role"], registry=registry
    )
    workers: dict[str, list[dict]] = {}
    for worker in journal.workers():
        workers.setdefault(worker["worker"].split(":", 1)[0], []).append(worker)
    for role, entries in workers.items():
        live.labels(role).set(any(entry["live"] for entry in entries))
        ready.labels(role).set(any(entry["live"] and entry["status"] == "ready" for entry in entries))
    states = Gauge(
        "bagel_work_items", "Durable work by domain and state", ["domain", "state"], registry=registry
    )
    with database_session(engine) as session:
        for domain, model in (
            ("research", m.ResearchRun),
            ("strategy", m.StrategyRun),
            ("data", m.DataJob),
        ):
            for state, count in session.execute(select(model.state, func.count()).group_by(model.state)):
                states.labels(domain, state).set(count)
        orders = (
            session.execute(
                select(
                    func.count().label("count"),
                    func.coalesce(func.sum(m.OrderIntent.reserved_notional), 0).label("reserved"),
                    func.count()
                    .filter(m.OrderIntent.state.in_(("dispatching", "unresolved")))
                    .label("unresolved"),
                ).where(~m.OrderIntent.state.in_(("rejected", "terminal", "expired")))
            )
            .mappings()
            .one()
        )
    assert orders
    for name in ("count", "reserved", "unresolved"):
        Gauge("bagel_active_orders_" + name, "Account-wide active order " + name, registry=registry).set(
            orders[name]
        )
    return generate_latest(registry)
