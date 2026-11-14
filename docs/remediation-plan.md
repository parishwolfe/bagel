# November 13 remediation

Implementation acceptance gates (not a completion claim):

- [ ] All application database access uses SQLAlchemy mapped models and sessions.
- [ ] Alpaca ingestion publishes complete, versioned events directly to Redis.
- [ ] Live strategies consume Redis events independently of database persistence.
- [ ] The persistence consumer commits ORM writes before acknowledging Redis.
- [ ] Duplicate delivery, crash recovery, frame completeness and financial fencing pass offline smoke checks.
- [ ] Redis pressure/retention and durability limitations are explicit and observable.
- [ ] Ruff hooks and Pyright pass without suppressions or relaxed rules.
- [x] Delete all unit tests; do not create or maintain unit tests at all.
- [ ] Validate the actual implementation with structured logs and Docker exec.
- [ ] Preserve existing data/migrations, keep submission disabled, and leave the stack running.

Use temporary verification scripts and disposable local resources, not a new test
suite. Historical DDL remains immutable. Existing staged work is not discarded or
committed automatically. No hosted changes or real orders are part of remediation.
