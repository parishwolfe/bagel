from collections.abc import Iterable, Iterator
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Annotated, Literal

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy import Text, cast, select
from sqlalchemy.orm import Session

from market import models as m
from market.backtesting.engine import CashOrSplitEvent
from market.data.domain import MINUTE, NEW_YORK, Bar, TimeRange
from market.database import database_engine, require_local_database
from market.research.artifacts import Artifacts, canonical_json, digest_json

Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
BATCH_ROWS = 16_384
SCHEMA = pa.schema(
    [
        ("symbol", pa.string()),
        ("timestamp", pa.timestamp("us", tz="UTC")),
        *[(field, pa.decimal128(28, 10)) for field in ("open", "high", "low", "close")],
        ("volume", pa.decimal128(28, 8)),
    ]
)


class Series(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    instrument: str
    source: str
    feed: str
    series_id: int | None = None


class DatasetManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    format: Literal["equity-minute-parquet-v1", "equity-minute-parquet-v2"] = "equity-minute-parquet-v1"
    availability: Literal["synthetic", "revised_history"]
    sha256: Digest
    series: tuple[Series, ...]
    rows: int = Field(gt=0)
    first_bar: AwareDatetime
    end_exclusive: AwareDatetime
    currency: Literal["USD"] = "USD"
    adjustment: Literal["raw"] = "raw"
    session: Literal["regular"] = "regular"
    actions: tuple[CashOrSplitEvent, ...] = ()
    revision_manifest_sha256: Digest | None = None
    legacy_unarchived_rows: int = Field(default=0, ge=0)

    @property
    def id(self) -> str:
        payload = self.model_dump(mode="json")
        if self.format == "equity-minute-parquet-v1":
            # Preserve every pre-existing dataset identity after adding v2 metadata.
            payload.pop("revision_manifest_sha256")
            payload.pop("legacy_unarchived_rows")
        return digest_json(payload)


def freeze(
    artifacts: Artifacts,
    bars: Iterable[Bar],
    series: tuple[Series, ...],
    availability: Literal["synthetic", "revised_history"],
    actions: tuple[CashOrSplitEvent, ...] = (),
    *,
    row_budget: int | None = None,
) -> DatasetManifest:
    allowed = {entry.instrument for entry in series}
    if not allowed or len(allowed) != len(series):
        raise ValueError("Select exactly one series per instrument")
    if any(action.instrument not in allowed for action in actions):
        raise ValueError("Corporate action instrument is outside this dataset")
    previous: tuple[datetime, str] | None = None
    first: datetime | None = None
    seen: set[str] = set()
    batch = []
    rows = 0
    with artifacts.temporary() as path:
        with pq.ParquetWriter(path, SCHEMA, compression="zstd") as writer:
            for bar in bars:
                key = (bar.timestamp, bar.symbol)
                if bar.symbol not in allowed or (previous is not None and key <= previous):
                    raise ValueError("Bars must have declared instruments and unique sorted keys")
                first = first or bar.timestamp
                previous = key
                seen.add(bar.symbol)
                rows += 1
                if row_budget is not None and rows > row_budget:
                    raise ValueError("Dataset exceeds its requested row resource budget")
                batch.append({field: getattr(bar, field) for field in SCHEMA.names})
                if len(batch) == BATCH_ROWS:
                    writer.write_table(pa.Table.from_pylist(batch, schema=SCHEMA))
                    batch.clear()
            if batch:
                writer.write_table(pa.Table.from_pylist(batch, schema=SCHEMA))
        if first is None or previous is None or seen != allowed:
            raise ValueError("Each selected series needs bars within the export window")
        digest = artifacts.publish(path)
    return DatasetManifest(
        availability=availability,
        sha256=digest,
        series=tuple(sorted(series, key=lambda s: s.instrument)),
        rows=rows,
        first_bar=first,
        end_exclusive=previous[0] + MINUTE,
        actions=tuple(sorted(actions, key=lambda a: (a.effective_at, a.instrument, a.kind))),
    )


def read_bars(artifacts: Artifacts, manifest: DatasetManifest) -> Iterator[Bar]:
    if manifest.format == "equity-minute-parquet-v2":
        if manifest.revision_manifest_sha256 is None:
            raise ValueError("Dataset revision manifest is missing")
        artifacts.read(manifest.revision_manifest_sha256)
    with pq.ParquetFile(artifacts.read(manifest.sha256)) as parquet:
        if parquet.schema_arrow != SCHEMA or parquet.metadata.num_rows != manifest.rows:
            raise ValueError("Parquet schema or row count does not match the manifest")
        for batch in parquet.iter_batches(batch_size=BATCH_ROWS):
            for row in batch.to_pylist():
                yield Bar(**row, vwap=None, trade_count=None)


def export_local(
    dsn: str,
    artifacts: Artifacts,
    series_ids: list[int],
    window: TimeRange,
    actions: tuple[CashOrSplitEvent, ...] = (),
    *,
    row_budget: int | None = None,
) -> DatasetManifest:
    require_local_database(dsn)
    if not series_ids or len(set(series_ids)) != len(series_ids):
        raise ValueError("Supply a nonempty set of unique series IDs")
    engine = database_engine(dsn, application_name="bagel-export", pool_size=1, timeout=5)
    try:
        with (
            engine.connect().execution_options(
                isolation_level="REPEATABLE READ", postgresql_readonly=True
            ) as connection,
            Session(bind=connection) as session,
            session.begin(),
        ):
            metadata = session.execute(
                select(m.Series.id, cast(m.Series.instrument_id, Text), m.Source.code, m.Series.feed)
                .join(m.Source, m.Source.id == m.Series.source_id)
                .join(m.Instrument, m.Instrument.id == m.Series.instrument_id)
                .where(
                    m.Series.id.in_(series_ids),
                    m.Series.interval_seconds == 60,
                    m.Series.currency == "USD",
                    m.Series.adjustment == "raw",
                    m.Instrument.asset_class == "us_equity",
                )
            ).all()
            if len(metadata) != len(series_ids):
                raise ValueError("Every selected series must be raw USD one-minute equity data")
            with session.execute(
                select(
                    cast(m.Series.instrument_id, Text).label("symbol"),
                    m.Bar.bar_start.label("timestamp"),
                    m.Bar.open,
                    m.Bar.high,
                    m.Bar.low,
                    m.Bar.close,
                    m.Bar.volume,
                    m.Bar.series_id,
                    m.Bar.revision_id,
                )
                .join(m.Series, m.Series.id == m.Bar.series_id)
                .where(
                    m.Bar.series_id.in_(series_ids),
                    m.Bar.bar_start >= window.start,
                    m.Bar.bar_start < window.end,
                    m.Bar.session == "regular",
                )
                .order_by(m.Bar.bar_start, m.Series.instrument_id)
                .execution_options(yield_per=BATCH_ROWS)
            ) as result:
                with artifacts.temporary() as reference_path:
                    legacy_rows = 0
                    with reference_path.open("wb") as references:

                        def bars() -> Iterator[Bar]:
                            nonlocal legacy_rows
                            for mapping in result.mappings():
                                row = dict(mapping)
                                revision = row.pop("revision_id")
                                series_id = row.pop("series_id")
                                legacy_rows += int(revision is None)
                                references.write(
                                    canonical_json(
                                        {
                                            "series_id": series_id,
                                            "bar_start": row["timestamp"],
                                            "revision_id": revision,
                                        }
                                    )
                                    + b"\n"
                                )
                                yield Bar(**row, vwap=None, trade_count=None)

                        manifest = freeze(
                            artifacts,
                            bars(),
                            tuple(
                                Series(series_id=row[0], instrument=row[1], source=row[2], feed=row[3])
                                for row in metadata
                            ),
                            "revised_history",
                            actions,
                            row_budget=row_budget,
                        )
                    reference_digest = artifacts.publish(reference_path)
                return manifest.model_copy(
                    update={
                        "format": "equity-minute-parquet-v2",
                        "revision_manifest_sha256": reference_digest,
                        "legacy_unarchived_rows": legacy_rows,
                    }
                )
    finally:
        engine.dispose()


def synthetic_fixture(artifacts: Artifacts, sessions: int) -> DatasetManifest:
    names = ("fixture-alpha", "fixture-beta")

    def bars() -> Iterator[Bar]:
        day = date(2026, 1, 5)
        for session in range(sessions):
            while day.weekday() >= 5:
                day += timedelta(days=1)
            start = datetime.combine(day, time(9, 30), NEW_YORK).astimezone(UTC)
            for minute in range(180):
                for offset, name in enumerate(names):
                    phase = (minute + 9 * offset + 5 * session) % 48
                    wave = min(phase, 48 - phase)
                    opening = Decimal(80 + 20 * offset) + Decimal(wave) / 4
                    closing = opening + (Decimal("0.15") if phase < 24 else Decimal("-0.15"))
                    yield Bar(
                        name,
                        start + minute * MINUTE,
                        opening,
                        max(opening, closing),
                        min(opening, closing),
                        closing,
                        Decimal(10000),
                        None,
                        None,
                    )
            day += timedelta(days=1)

    return freeze(
        artifacts,
        bars(),
        tuple(Series(instrument=name, source="synthetic", feed="fixture-v1") for name in names),
        "synthetic",
    )
