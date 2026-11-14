"""Offline Redis-first capacity/recovery benchmark with run-scoped ORM cleanup.

Uses unique synthetic provider identities and Redis keys, never a provider API or
broker. It does not reset volumes, create unit tests, or delete unrelated records.
"""

import argparse
import json
import resource
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast
from uuid import uuid4

from sqlalchemy import delete, func, select, update

from market import models as m
from market.data.domain import Bar, Instrument, Observation, Origin
from market.data.storage import PostgresStore
from market.database import database_session, require_local_database
from market.events.bus import BusSettings, MarketBus
from market.events.config import EventSettings
from market.events.consumer import PERSISTENCE_GROUP, persist_decisions_once, persist_once
from market.events.contracts import market_event
from market.strategies.core import StrategySpec
from market.strategies.service import Strategies, StrategyRunSpec
from market.strategies.worker import StrategyWorker


def benchmark(instruments: int, minutes: int, with_strategies: bool) -> dict:
    settings = EventSettings()
    require_local_database(settings.dsn())
    nonce = uuid4().hex
    source, feed = "fixture-" + nonce, "fixture-v2"
    prefix = "bagel:benchmark:" + nonce
    bus = MarketBus(BusSettings(market_stream=prefix + ":market", decision_stream=prefix + ":decisions"))
    symbols = [f"SIM{i:06d}" for i in range(instruments)]
    runs, series, instrument_ids = [], {}, []
    started = datetime.now(UTC)
    stamp = started.replace(second=0, microsecond=0) - timedelta(minutes=minutes + 2)
    try:
        with PostgresStore(settings.dsn()) as store:
            baseline = store.database_bytes()
            catalogue = [
                Instrument(str(i), symbol, "Synthetic Redis-first benchmark")
                for i, symbol in enumerate(symbols)
            ]
            series = store.resolve_series(catalogue, feed, source)
            with database_session(store.engine) as session:
                instrument_ids = list(
                    session.scalars(select(m.Series.instrument_id).where(m.Series.id.in_(series.values())))
                )
            seed = [
                Observation(
                    Bar(
                        symbol,
                        stamp - timedelta(minutes=1),
                        Decimal(100),
                        Decimal(101),
                        Decimal(99),
                        Decimal(100),
                        Decimal(10000),
                        None,
                        100,
                    ),
                    "regular",
                    Origin.LIVE,
                    started,
                    started,
                )
                for symbol in symbols
            ]
            for begin in range(0, len(seed), 500):
                store.write_live(series, seed[begin : begin + 500])
            service = Strategies(store.engine)
            if with_strategies:
                for name in ("sma_cross", "cross_section_momentum"):
                    identity = service.create(
                        StrategyRunSpec(
                            name="redis-benchmark-" + nonce + name,
                            source=source,
                            feed=feed,
                            strategy=StrategySpec(name=name, fast=2, slow=3),
                            decision_lifetime_seconds=3600,
                            frame_delay_seconds=1,
                        )
                    )
                    service.control(identity, True)
                    with database_session(store.engine) as session:
                        session.execute(
                            update(m.StrategyRun)
                            .where(m.StrategyRun.id == identity)
                            .values(activated_at=stamp)
                        )
                    runs.append(identity)
            worker = StrategyWorker(store.engine, uuid4(), bus, run_ids=tuple(runs)) if runs else None
            bus.group(PERSISTENCE_GROUP)
            first = None
            begin = time.monotonic()
            for minute in range(minutes):
                for ordinal, symbol in enumerate(symbols):
                    now = datetime.now(UTC)
                    price = Decimal(100) + Decimal(ordinal % 100) / 100 + Decimal(minute) / 100
                    observation = Observation(
                        Bar(
                            symbol,
                            stamp + timedelta(minutes=minute),
                            price,
                            price + 1,
                            price - 1,
                            price,
                            Decimal(10000),
                            None,
                            100,
                        ),
                        "regular",
                        Origin.LIVE,
                        now,
                        now,
                    )
                    event = market_event(
                        observation, provider=source, feed=feed, provider_instrument_id=str(ordinal)
                    )
                    bus.publish(event)
                    first = first or event
                    if worker and ordinal % 500 == 499:
                        worker.step()
            publish_seconds = time.monotonic() - begin
            deadline = time.monotonic() + 600
            states = []
            while worker and time.monotonic() < deadline:
                worker.step()
                states = [
                    json.loads(cast(str, bus.redis.get(worker._keys(config)[0])))
                    for config in worker.configurations
                ]
                if len(states) == len(runs) and all(s["frames"] == minutes for s in states):
                    break
            if worker:
                assert len(states) == 2 and all(s["frames"] == minutes for s in states), (
                    "strategy_catchup_deadline"
                )
            with database_session(store.engine) as session:
                before = session.scalar(
                    select(func.count()).select_from(m.Bar).where(m.Bar.series_id.in_(series.values()))
                )
                signals_before = session.scalar(
                    select(func.count()).select_from(m.StrategySignal).where(m.StrategySignal.run_id.in_(runs))
                )
            assert before == instruments and signals_before == 0, "database_writer_was_not_independent"
            assert first is not None
            bus.publish(first)
            if worker:
                worker.step()

            class CrashBeforeAck(RuntimeError):
                pass

            def crash():
                raise CrashBeforeAck

            try:
                persist_once(bus, store, "first", before_ack=crash)
                raise AssertionError("crash_injection_not_reached")
            except CrashBeforeAck:
                pass
            pending = cast(
                list, bus.redis.xpending_range(bus.settings.market_stream, PERSISTENCE_GROUP, "-", "+", 500)
            )
            assert pending
            bus.redis.xclaim(
                bus.settings.market_stream,
                PERSISTENCE_GROUP,
                "replacement",
                0,
                [r["message_id"] for r in pending],
            )
            begin = time.monotonic()
            persisted = 0
            while count := persist_once(bus, store, "replacement"):
                persisted += count
            persistence_seconds = time.monotonic() - begin
            while persist_decisions_once(bus, store, "replacement"):
                pass
            with database_session(store.engine) as session:
                bars = session.scalar(
                    select(func.count()).select_from(m.Bar).where(m.Bar.series_id.in_(series.values()))
                )
                revisions = session.scalar(
                    select(func.count())
                    .select_from(m.BarRevision)
                    .where(m.BarRevision.series_id.in_(series.values()))
                )
                receipts = session.scalar(
                    select(func.count())
                    .select_from(m.MarketStreamReceipt)
                    .where(m.MarketStreamReceipt.stream == bus.settings.market_stream)
                )
                signals = session.scalar(
                    select(func.count()).select_from(m.StrategySignal).where(m.StrategySignal.run_id.in_(runs))
                )
            assert bars == revisions == instruments * (minutes + 1)
            assert receipts == instruments * minutes + 1
            assert signals == minutes * len(runs)
            assert (
                cast(dict, bus.redis.xpending(bus.settings.market_stream, PERSISTENCE_GROUP))["pending"] == 0
            )
            report = dict(
                architecture="redis_first_orm",
                instruments=instruments,
                rows=instruments * minutes,
                seed_rows=instruments,
                publication_seconds=publish_seconds,
                publication_rows_per_second=instruments * minutes / publish_seconds,
                persistence_seconds=persistence_seconds,
                strategy_frames=signals,
                writer_stopped_strategy_progress="passed",
                commit_before_ack_replacement="passed",
                duplicate_delivery="passed",
                database_growth_bytes=store.database_bytes() - baseline,
                peak_process_rss_mib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
                provider_calls=0,
                broker_mutations=0,
                utc_start=started.isoformat(),
                utc_end=datetime.now(UTC).isoformat(),
            )
            return report
    finally:
        with PostgresStore(settings.dsn()) as cleanup:
            with database_session(cleanup.engine) as session:
                if runs:
                    for model in (m.StrategyInput, m.StrategyFrameBar, m.StrategySignal, m.StrategyMember):
                        session.execute(delete(model).where(model.run_id.in_(runs)))
                    session.execute(delete(m.StrategyRun).where(m.StrategyRun.id.in_(runs)))
                session.execute(
                    delete(m.MarketStreamReceipt).where(
                        m.MarketStreamReceipt.stream == bus.settings.market_stream
                    )
                )
                if series:
                    for model in (m.Bar, m.BarRevision, m.IngestedUniverse):
                        session.execute(delete(model).where(model.series_id.in_(series.values())))
                    session.execute(delete(m.Series).where(m.Series.id.in_(series.values())))
                source_id = session.scalar(select(m.Source.id).where(m.Source.code == source))
                if source_id is not None:
                    session.execute(
                        delete(m.InstrumentMapping).where(m.InstrumentMapping.source_id == source_id)
                    )
                    session.execute(delete(m.Source).where(m.Source.id == source_id))
                if instrument_ids:
                    session.execute(delete(m.Instrument).where(m.Instrument.id.in_(instrument_ids)))
        keys = list(bus.redis.scan_iter(match=prefix + "*"))
        for begin in range(0, len(keys), 500):
            bus.redis.unlink(*keys[begin : begin + 500])
        bus.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instruments", type=int, default=128)
    parser.add_argument("--minutes", type=int, default=5)
    parser.add_argument("--with-strategies", action="store_true")
    args = parser.parse_args()
    if args.instruments < 1 or not 1 <= args.minutes <= 55:
        parser.error("Use positive instruments and 1-55 modeled minutes within decision expiry")
    print(json.dumps(benchmark(args.instruments, args.minutes, args.with_strategies), sort_keys=True))


if __name__ == "__main__":
    main()
