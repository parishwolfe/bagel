# Market-data component

`domain` owns validated immutable bars and UTC intervals. `ports` defines provider
capabilities. `providers.alpaca` uses alpaca-py request models and implements the
documented REST pagination and WebSocket protocol explicitly: this preserves
decimal JSON prices, makes authentication failures visible, and reports disconnects.

`historical` orchestrates bounded daily ranges. `storage` owns reference identity,
series isolation, atomic upserts/checkpoints, and session locks. `runtime` runs
collection, bounded buffering/batch writing, REST repair, and lock heartbeats as
separate tasks. `observability` contains health and operational metrics. `cli`
composes these pieces; tools never implement another ingestion path.

To add a provider, implement `HistoricalBars`, `InstrumentLookup`,
`TradingCalendar`, and `StreamingBars`; emit domain bars rather than SDK objects.
Register the source and map provider identifiers to canonical instruments using
`PostgresStore.resolve_series(..., canonical_instruments=...)`. Leave different
feeds in separate series. Add provider-contract fixtures before enabling it in
the composition layer. New event types such as quotes and trades should get
typed event tables rather than nullable columns in the bars table.

The first release intentionally has no event broker. PostgreSQL range checkpoints
plus provider replay are the recovery mechanism. When ingestion fans out to
multiple downstream consumers or tick volume grows, a durable broker can be
introduced between provider adapters and writers without changing the domain.
