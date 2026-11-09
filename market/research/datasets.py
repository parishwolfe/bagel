from collections.abc import Iterable, Iterator
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Annotated, Literal

import psycopg
import pyarrow as pa
import pyarrow.parquet as pq
from psycopg.rows import dict_row
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from market.backtesting.engine import CashOrSplitEvent
from market.data.domain import MINUTE, NEW_YORK, Bar, TimeRange
from market.research.artifacts import Artifacts, digest_json
from market.research.config import require_local_database

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

    format: Literal["equity-minute-parquet-v1"] = "equity-minute-parquet-v1"
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

    @property
    def id(self) -> str:
        return digest_json(self.model_dump(mode="json"))


def freeze(
    artifacts: Artifacts,
    bars: Iterable[Bar],
    series: tuple[Series, ...],
    availability: Literal["synthetic", "revised_history"],
    actions: tuple[CashOrSplitEvent, ...] = (),
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
) -> DatasetManifest:
    require_local_database(dsn)
    if not series_ids or len(series_ids) > 100 or len(set(series_ids)) != len(series_ids):
        raise ValueError("Supply 1–100 unique series IDs")
    with psycopg.connect(dsn, connect_timeout=5) as conn:
        conn.execute("set transaction isolation level repeatable read read only")
        metadata = conn.execute(
            """select s.id as series_id, s.instrument_id::text as instrument, src.code as source,
                      s.feed
                 from market_data.series s
                 join market_data.sources src on src.id = s.source_id
                 join market_data.instruments i on i.id = s.instrument_id
                where s.id = any(%s) and s.interval_seconds = 60 and s.currency = 'USD'
                  and s.adjustment = 'raw' and i.asset_class = 'us_equity'""",
            (series_ids,),
        ).fetchall()
        if len(metadata) != len(series_ids):
            raise ValueError("Every selected series must be raw USD one-minute equity data")
        with conn.cursor(name="research_export", row_factory=dict_row) as rows:
            rows.execute(
                """select s.instrument_id::text as symbol, b.bar_start as timestamp,
                          b.open, b.high, b.low, b.close, b.volume
                     from market_data.bars b join market_data.series s on s.id = b.series_id
                    where b.series_id = any(%s) and b.bar_start >= %s and b.bar_start < %s
                      and b.session = 'regular'
                    order by b.bar_start, s.instrument_id""",
                (series_ids, window.start, window.end),
            )
            return freeze(
                artifacts,
                (Bar(**row, vwap=None, trade_count=None) for row in rows),
                tuple(
                    Series(series_id=row[0], instrument=row[1], source=row[2], feed=row[3])
                    for row in metadata
                ),
                "revised_history",
                actions,
            )


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
