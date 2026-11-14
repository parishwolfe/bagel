"""ORM mappings for the existing public schema. Migrations alone own DDL."""

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Date,
    DateTime,
    FetchedValue,
    Numeric,
    Text,
    false,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class PublicRow(Base):
    __abstract__ = True
    __table_args__ = {"schema": "public"}


def generated():
    return mapped_column(server_default=FetchedValue())


class Source(PublicRow):
    __tablename__ = "sources"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    code: Mapped[str]


class Instrument(PublicRow):
    __tablename__ = "instruments"
    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=FetchedValue())
    name: Mapped[str]
    asset_class: Mapped[str]
    currency: Mapped[str]


class InstrumentMapping(PublicRow):
    __tablename__ = "instrument_mappings"
    source_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    provider_instrument_id: Mapped[str] = mapped_column(primary_key=True)
    valid_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, server_default=FetchedValue()
    )
    instrument_id: Mapped[UUID]
    symbol: Mapped[str]
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Series(PublicRow):
    __tablename__ = "series"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(BigInteger)
    instrument_id: Mapped[UUID]
    feed: Mapped[str]
    interval_seconds: Mapped[int]
    currency: Mapped[str]
    adjustment: Mapped[str]


class Bar(PublicRow):
    __tablename__ = "bars"
    series_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    bar_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    open: Mapped[Decimal] = mapped_column(Numeric)
    high: Mapped[Decimal] = mapped_column(Numeric)
    low: Mapped[Decimal] = mapped_column(Numeric)
    close: Mapped[Decimal] = mapped_column(Numeric)
    volume: Mapped[Decimal] = mapped_column(Numeric)
    vwap: Mapped[Decimal | None] = mapped_column(Numeric)
    trade_count: Mapped[int | None] = mapped_column(BigInteger)
    session: Mapped[str]
    origin: Mapped[str]
    quality_rank: Mapped[int] = generated()
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=FetchedValue())
    revision_id: Mapped[UUID | None]


class BarRevision(PublicRow):
    __tablename__ = "bar_revisions"
    series_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    bar_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    event_id: Mapped[UUID] = mapped_column(primary_key=True)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    envelope: Mapped[dict] = mapped_column(JSONB)


class IngestedUniverse(PublicRow):
    __tablename__ = "ingested_universe"
    series_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    first_received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class IngestionRun(PublicRow):
    __tablename__ = "ingestion_runs"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    mode: Mapped[str]
    symbols: Mapped[list[str]] = mapped_column(ARRAY(Text))
    requested_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    requested_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=FetchedValue())
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = generated()
    error_code: Mapped[str | None]


class CoverageRange(PublicRow):
    __tablename__ = "ranges"
    series_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    range_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    range_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    run_id: Mapped[UUID]
    status: Mapped[str] = generated()
    bar_count: Mapped[int] = generated()
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StreamState(PublicRow):
    __tablename__ = "stream_state"
    stream_key: Mapped[str] = mapped_column(primary_key=True)
    repair_cursor: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=FetchedValue())


class Watchlist(PublicRow):
    __tablename__ = "watchlists"
    stream_key: Mapped[str] = mapped_column(primary_key=True)
    symbols: Mapped[list[str]] = mapped_column(ARRAY(Text))
    revision: Mapped[int] = generated()
    config_signature: Mapped[str | None]
    loaded_revision: Mapped[int | None]
    attempt_years: Mapped[int | None]
    history_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    history_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = generated()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=FetchedValue())


class EventJournal(PublicRow):
    __tablename__ = "event_journal"
    event_id: Mapped[UUID] = mapped_column(primary_key=True)
    topic: Mapped[str]
    partition_key: Mapped[str]
    body: Mapped[dict] = mapped_column(JSONB)
    sequence: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=FetchedValue())
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PublicationCounter(PublicRow):
    __tablename__ = "publication_counter"
    singleton: Mapped[bool] = mapped_column(primary_key=True)
    last_sequence: Mapped[int] = mapped_column(BigInteger)


class ConsumerCheckpoint(PublicRow):
    __tablename__ = "consumer_checkpoints"
    consumer: Mapped[str] = mapped_column(primary_key=True)
    last_sequence: Mapped[int] = generated()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=FetchedValue())


class WorkerHeartbeat(PublicRow):
    __tablename__ = "worker_heartbeats"
    worker: Mapped[str] = mapped_column(primary_key=True)
    process_id: Mapped[UUID]
    status: Mapped[str]
    details: Mapped[dict] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=FetchedValue())


class DataConsumerState(PublicRow):
    __tablename__ = "data_consumer_state"
    consumer: Mapped[str] = mapped_column(primary_key=True)
    event_count: Mapped[int] = generated()
    last_sequence: Mapped[int] = generated()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=FetchedValue())


class Dataset(PublicRow):
    __tablename__ = "datasets"
    id: Mapped[str] = mapped_column(primary_key=True)
    manifest: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=FetchedValue())


class ResearchEngine(PublicRow):
    __tablename__ = "research_engines"
    id: Mapped[str] = mapped_column(primary_key=True)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB)
    sources: Mapped[dict] = mapped_column(JSONB)
    source_sha256: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=FetchedValue())


class ResearchRun(PublicRow):
    __tablename__ = "research_runs"
    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=FetchedValue())
    request_hash: Mapped[str]
    dataset_id: Mapped[str]
    engine_id: Mapped[str]
    spec: Mapped[dict] = mapped_column(JSONB)
    actor: Mapped[UUID | None]
    state: Mapped[str] = generated()
    attempts: Mapped[int] = generated()
    lease_token: Mapped[UUID | None]
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result_sha256: Mapped[str | None]
    summary: Mapped[dict | None] = mapped_column(JSONB)
    error_code: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=FetchedValue())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=FetchedValue())


class Attempt(PublicRow):
    __tablename__ = "attempts"
    run_id: Mapped[UUID] = mapped_column(primary_key=True)
    attempt: Mapped[int] = mapped_column(primary_key=True)
    token: Mapped[UUID]
    state: Mapped[str]
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=FetchedValue())
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None]


class Batch(PublicRow):
    __tablename__ = "batches"
    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=FetchedValue())
    request_hash: Mapped[str]
    actor: Mapped[UUID | None]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=FetchedValue())


class BatchRun(PublicRow):
    __tablename__ = "batch_runs"
    batch_id: Mapped[UUID] = mapped_column(primary_key=True)
    ordinal: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[UUID]


class Experiment(PublicRow):
    __tablename__ = "experiments"
    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=FetchedValue())
    request_hash: Mapped[str]
    dataset_id: Mapped[str]
    spec: Mapped[dict] = mapped_column(JSONB)
    total_trials: Mapped[int] = mapped_column(BigInteger)
    generated_trials: Mapped[int] = generated()
    state: Mapped[str] = generated()
    engine_id: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=FetchedValue())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=FetchedValue())


class ExperimentTrial(PublicRow):
    __tablename__ = "experiment_trials"
    experiment_id: Mapped[UUID] = mapped_column(primary_key=True)
    ordinal: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[UUID | None]
    normalized_spec: Mapped[dict] = mapped_column(JSONB)
    error_code: Mapped[str | None]


class DataJob(PublicRow):
    __tablename__ = "data_jobs"
    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=FetchedValue())
    request_hash: Mapped[str]
    provider: Mapped[str]
    spec: Mapped[dict] = mapped_column(JSONB)
    state: Mapped[str] = generated()
    attempts: Mapped[int] = generated()
    lease_token: Mapped[UUID | None]
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict | None] = mapped_column(JSONB)
    error_code: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=FetchedValue())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=FetchedValue())


class TradingAccount(PublicRow):
    __tablename__ = "trading_accounts"
    account_id: Mapped[UUID] = mapped_column(primary_key=True)
    owner_token: Mapped[UUID]
    epoch: Mapped[int] = generated()
    paused: Mapped[bool] = generated()
    run_id: Mapped[UUID | None]
    armed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    policy: Mapped[dict] = mapped_column(JSONB)
    reconciled: Mapped[bool] = generated()
    reason: Mapped[str] = generated()
    opening_cash: Mapped[Decimal | None] = mapped_column(Numeric)
    cash: Mapped[Decimal] = mapped_column(Numeric, server_default=FetchedValue())
    realized_pnl: Mapped[Decimal] = mapped_column(Numeric, server_default=FetchedValue())
    fees: Mapped[Decimal] = mapped_column(Numeric, server_default=FetchedValue())
    positions: Mapped[dict] = mapped_column(JSONB, server_default=FetchedValue())
    attribution: Mapped[dict] = mapped_column(JSONB, server_default=FetchedValue())
    equity_peak: Mapped[Decimal | None] = mapped_column(Numeric)
    session_equity: Mapped[Decimal | None] = mapped_column(Numeric)
    session_date: Mapped[date | None] = mapped_column(Date)
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    snapshot: Mapped[dict] = mapped_column(JSONB, server_default=FetchedValue())
    stream_connected: Mapped[bool] = generated()
    last_activity_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_activity_id: Mapped[str | None]
    rebuild_required: Mapped[bool] = generated()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=FetchedValue())


class TradingCommand(PublicRow):
    __tablename__ = "trading_commands"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    request_hash: Mapped[str]
    account_id: Mapped[UUID]
    action: Mapped[str]
    payload: Mapped[dict] = mapped_column(JSONB)
    state: Mapped[str] = generated()
    result: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=FetchedValue())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OrderIntent(PublicRow):
    __tablename__ = "order_intents"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    account_id: Mapped[UUID]
    decision_id: Mapped[UUID]
    strategy_id: Mapped[UUID | None]
    run_id: Mapped[UUID]
    signal_id: Mapped[UUID | None]
    client_order_id: Mapped[str]
    decision_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict] = mapped_column(JSONB)
    inputs: Mapped[list] = mapped_column(JSONB)
    state: Mapped[str]
    reserved_notional: Mapped[Decimal] = mapped_column(Numeric, server_default=FetchedValue())
    dispatch_count: Mapped[int] = generated()
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    broker_order_id: Mapped[UUID | None]
    reason: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=FetchedValue())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=FetchedValue())


class RiskDecision(PublicRow):
    __tablename__ = "risk_decisions"
    intent_id: Mapped[UUID] = mapped_column(primary_key=True)
    approved: Mapped[bool]
    policy_version: Mapped[str]
    policy: Mapped[dict] = mapped_column(JSONB)
    inputs: Mapped[dict] = mapped_column(JSONB)
    reason: Mapped[str]
    reservation: Mapped[Decimal] = mapped_column(Numeric)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=FetchedValue())


class BrokerOrder(PublicRow):
    __tablename__ = "broker_orders"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    account_id: Mapped[UUID]
    root_intent: Mapped[UUID | None]
    client_order_id: Mapped[str]
    symbol: Mapped[str]
    state: Mapped[str]
    quantity: Mapped[Decimal | None] = mapped_column(Numeric)
    filled_quantity: Mapped[Decimal] = mapped_column(Numeric)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict] = mapped_column(JSONB)
    parent_order_id: Mapped[UUID | None]


class AccountActivity(PublicRow):
    __tablename__ = "account_activities"
    account_id: Mapped[UUID] = mapped_column(primary_key=True)
    activity_id: Mapped[str] = mapped_column(primary_key=True)
    activity_type: Mapped[str]
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict] = mapped_column(JSONB)
    payload_sha256: Mapped[str]
    applied: Mapped[bool] = generated()


class BrokerSyncCheckpoint(PublicRow):
    __tablename__ = "broker_sync_checkpoints"
    account_id: Mapped[UUID] = mapped_column(primary_key=True)
    activity_cursor: Mapped[str | None]
    activity_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    initial_activities_complete: Mapped[bool] = generated()
    order_cursor: Mapped[str | None]
    initial_orders_complete: Mapped[bool] = generated()


class TradingAudit(PublicRow):
    __tablename__ = "trading_audit"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    account_id: Mapped[UUID]
    event_id: Mapped[UUID]
    event_type: Mapped[str]
    body: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=FetchedValue())


class LedgerEntry(PublicRow):
    __tablename__ = "ledger_entries"
    account_id: Mapped[UUID] = mapped_column(primary_key=True)
    activity_id: Mapped[str] = mapped_column(primary_key=True)
    attribution: Mapped[str]
    symbol: Mapped[str | None]
    quantity: Mapped[Decimal] = mapped_column(Numeric)
    price: Mapped[Decimal] = mapped_column(Numeric)
    cash_delta: Mapped[Decimal] = mapped_column(Numeric)
    realized_pnl: Mapped[Decimal] = mapped_column(Numeric)
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class StrategyRun(PublicRow):
    __tablename__ = "strategy_runs"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    request_hash: Mapped[str]
    spec: Mapped[dict] = mapped_column(JSONB)
    engine_id: Mapped[str]
    state: Mapped[str] = generated()
    reason: Mapped[str] = generated()
    owner_token: Mapped[UUID | None]
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cursor: Mapped[int] = generated()
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_frame: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    checkpoint: Mapped[dict] = mapped_column(JSONB, server_default=FetchedValue())
    warmed: Mapped[bool] = generated()
    frames: Mapped[int] = generated()
    skipped_frames: Mapped[int] = generated()
    late_revisions: Mapped[int] = generated()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=FetchedValue())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=FetchedValue())


class StrategyMember(PublicRow):
    __tablename__ = "strategy_members"
    run_id: Mapped[UUID] = mapped_column(primary_key=True)
    series_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    instrument_id: Mapped[UUID]
    symbol: Mapped[str]


class StrategyFrameBar(PublicRow):
    __tablename__ = "strategy_frame_bars"
    run_id: Mapped[UUID] = mapped_column(primary_key=True)
    bar_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    series_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    sequence: Mapped[int] = mapped_column(BigInteger)
    envelope: Mapped[dict] = mapped_column(JSONB)


class StrategySignal(PublicRow):
    __tablename__ = "strategy_signals"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    run_id: Mapped[UUID]
    bar_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decision_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    weights: Mapped[dict] = mapped_column(JSONB)
    inputs: Mapped[list] = mapped_column(JSONB)
    missing_members: Mapped[int]
    execution_eligible: Mapped[bool]
    state_sha256: Mapped[str]
    activation_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StrategyInput(PublicRow):
    __tablename__ = "strategy_inputs"
    run_id: Mapped[UUID] = mapped_column(primary_key=True)
    activation_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    bar_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    series_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    revision_id: Mapped[UUID]
    purpose: Mapped[str] = mapped_column(primary_key=True)


class StrategyTargetState(PublicRow):
    __tablename__ = "strategy_target_state"
    account_id: Mapped[UUID] = mapped_column(primary_key=True)
    strategy_id: Mapped[UUID] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(primary_key=True)
    activation_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    weight: Mapped[Decimal] = mapped_column(Numeric)
    quantity: Mapped[Decimal] = mapped_column(Numeric)
    sizing_signal_id: Mapped[UUID | None]
    sizing_decision_id: Mapped[UUID]
    sizing_price: Mapped[Decimal] = mapped_column(Numeric)
    sizing_equity: Mapped[Decimal] = mapped_column(Numeric)


class PaperAccount(PublicRow):
    __tablename__ = "paper_accounts"
    account_id: Mapped[UUID] = mapped_column(primary_key=True)
    status: Mapped[str]
    cash: Mapped[Decimal] = mapped_column(Numeric)
    equity: Mapped[Decimal] = mapped_column(Numeric)
    buying_power: Mapped[Decimal] = mapped_column(Numeric)
    trading_blocked: Mapped[bool]
    positions: Mapped[list] = mapped_column(JSONB)
    orders: Mapped[list] = mapped_column(JSONB)
    snapshot_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reconciled: Mapped[bool] = generated()
    reconciliation_reason: Mapped[str] = generated()


class PaperFillEvidence(PublicRow):
    __tablename__ = "paper_fill_evidence"
    account_id: Mapped[UUID] = mapped_column(primary_key=True)
    activity_id: Mapped[str] = mapped_column(primary_key=True)
    broker_order_id: Mapped[UUID]
    symbol: Mapped[str]
    side: Mapped[str]
    quantity: Mapped[Decimal] = mapped_column(Numeric)
    price: Mapped[Decimal] = mapped_column(Numeric)
    transaction_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PaperStreamEvidence(PublicRow):
    __tablename__ = "paper_stream_evidence"
    event_id: Mapped[str] = mapped_column(primary_key=True)
    account_id: Mapped[UUID]
    broker_order_id: Mapped[UUID]
    event_type: Mapped[str]
    broker_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=FetchedValue())


class SchemaMigration(PublicRow):
    __tablename__ = "bagel_schema_migrations"
    name: Mapped[str] = mapped_column(primary_key=True)
    sha256: Mapped[str]
    adopted: Mapped[bool] = mapped_column(server_default=false())
    applied_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )


class MarketStreamReceipt(PublicRow):
    __tablename__ = "market_stream_receipts"
    stream: Mapped[str] = mapped_column(primary_key=True)
    stream_id: Mapped[str] = mapped_column(primary_key=True)
    event_id: Mapped[UUID]
    persisted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=FetchedValue())


class PostgresExtension(Base):
    __tablename__ = "pg_extension"
    __table_args__ = {"schema": "pg_catalog"}
    oid: Mapped[int] = mapped_column(primary_key=True)
    extname: Mapped[str]
    extversion: Mapped[str]


class PostgresInheritance(Base):
    __tablename__ = "pg_inherits"
    __table_args__ = {"schema": "pg_catalog"}
    inhrelid: Mapped[int] = mapped_column(primary_key=True)
    inhparent: Mapped[int] = mapped_column(primary_key=True)


class PostgresRole(Base):
    __tablename__ = "pg_roles"
    __table_args__ = {"schema": "pg_catalog"}
    rolname: Mapped[str] = mapped_column(primary_key=True)
    rolsuper: Mapped[bool]
    rolcreatedb: Mapped[bool]
    rolcreaterole: Mapped[bool]
    rolreplication: Mapped[bool]
    rolbypassrls: Mapped[bool]
