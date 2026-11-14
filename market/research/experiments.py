"""Incremental, bounded parameter generation over the existing fenced run queue."""

from math import prod
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from market import models as m
from market.backtesting.engine import BacktestSpec
from market.database import database_session, row_dict
from market.events.commands import append_command
from market.research.artifacts import digest_json
from market.research.datasets import Digest
from market.research.queue import BatchSpec, Queue, engine_id
from market.strategies.core import StrategySpec

GENERATION_BATCH = 32


class ExperimentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_id: Digest
    strategies: tuple[StrategySpec, ...] = Field(min_length=1)
    base: BacktestSpec = Field(default_factory=BacktestSpec)
    grid: dict[str, tuple[int | str | bool, ...]] = Field(default_factory=dict)
    max_trials: int = Field(default=100_000, ge=1, le=10_000_000)
    max_pending: int = Field(default=64, ge=1, le=4096)

    @model_validator(mode="after")
    def validate_grid(self) -> Self:
        for key, values in self.grid.items():
            path = key.split(".")
            if len(path) != 2 or path[0] not in ("strategy", "risk", "fill_model") or not values:
                raise ValueError("Grid keys must address nonempty strategy, risk or fill_model fields")
            model = {
                "strategy": StrategySpec,
                "risk": type(self.base.risk),
                "fill_model": type(self.base.fill_model),
            }[path[0]]
            if path[1] not in model.model_fields or path[1] == "name":
                raise ValueError("Unknown grid parameter")
        if self.total > self.max_trials:
            raise ValueError("Experiment exceeds its explicit trial budget")
        return self

    @property
    def total(self) -> int:
        return len(self.strategies) * prod(len(v) for v in self.grid.values())

    def trial(self, ordinal: int) -> dict:
        if not 0 <= ordinal < self.total:
            raise ValueError("Trial ordinal is outside this experiment")
        keys = sorted(self.grid)
        per_strategy = prod(len(v) for v in self.grid.values())
        strategy_index, remainder = divmod(ordinal, per_strategy)
        result = self.base.model_dump(mode="json")
        result["strategy"] = self.strategies[strategy_index].model_dump(mode="json")
        for key in reversed(keys):
            remainder, index = divmod(remainder, len(self.grid[key]))
            group, field = key.split(".")
            result[group][field] = self.grid[key][index]
        return result


class Experiments:
    def __init__(self, queue: Queue, pending_budget: int = 256):
        self.queue = queue
        self.pending_budget = pending_budget

    def create(self, spec: ExperimentSpec) -> UUID:
        self.queue.dataset(spec.dataset_id)
        payload = spec.model_dump(mode="json")
        request_hash = digest_json((engine_id(), payload))
        with database_session(self.queue.engine) as session:
            statement = insert(m.Experiment).values(
                request_hash=request_hash,
                dataset_id=spec.dataset_id,
                spec=payload,
                total_trials=spec.total,
                engine_id=engine_id(),
            )
            identity = session.scalar(
                statement.on_conflict_do_update(
                    index_elements=[m.Experiment.request_hash],
                    set_={"request_hash": statement.excluded.request_hash},
                ).returning(m.Experiment.id)
            )
            assert identity is not None
            append_command(
                session,
                identity,
                "command.research.experiment",
                spec.dataset_id,
                {"experiment_id": str(identity)},
            )
            return identity

    def expand_once(self) -> bool:
        with database_session(self.queue.engine) as session:
            session.scalar(select(func.pg_advisory_xact_lock(74021812)))
            instance = session.scalar(
                select(m.Experiment)
                .where(
                    m.Experiment.state == "active",
                    m.Experiment.generated_trials < m.Experiment.total_trials,
                    m.Experiment.engine_id == engine_id(),
                )
                .order_by(m.Experiment.updated_at, m.Experiment.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            row = row_dict(instance) if instance else None
            if row is None:
                return False
            spec = ExperimentSpec.model_validate(row["spec"])
            pending = (
                session.scalar(
                    select(func.count())
                    .select_from(m.ExperimentTrial)
                    .join(m.ResearchRun, m.ResearchRun.id == m.ExperimentTrial.run_id)
                    .where(
                        m.ExperimentTrial.experiment_id == row["id"],
                        m.ResearchRun.state.in_(("queued", "running")),
                    )
                )
                or 0
            )
            global_pending = (
                session.scalar(
                    select(func.count())
                    .select_from(m.ResearchRun)
                    .where(m.ResearchRun.state.in_(("queued", "running")))
                )
                or 0
            )
            budget = min(
                GENERATION_BATCH,
                spec.max_pending - pending,
                self.pending_budget - global_pending,
            )
            if budget <= 0:
                session.execute(
                    update(m.Experiment)
                    .where(m.Experiment.id == row["id"])
                    .values(updated_at=func.clock_timestamp())
                )
                return False
            begin = row["generated_trials"]
            end = min(spec.total, begin + budget)
            for ordinal in range(begin, end):
                payload = spec.trial(ordinal)
                run_id = None
                error_code = None
                try:
                    validated = BacktestSpec.model_validate(payload)
                    batch = self.queue.enqueue(BatchSpec(dataset_id=spec.dataset_id, runs=(validated,)))
                    run_id = self.queue.batch(batch)["runs"][0]["id"]
                except (ValueError, ValidationError):
                    # Invalid combinations are trials too, not silently dropped.
                    error_code = "invalid_trial_configuration"
                session.add(
                    m.ExperimentTrial(
                        experiment_id=row["id"],
                        ordinal=ordinal,
                        run_id=run_id,
                        normalized_spec=payload,
                        error_code=error_code,
                    )
                )
            session.execute(
                update(m.Experiment)
                .where(m.Experiment.id == row["id"])
                .values(generated_trials=end, updated_at=func.clock_timestamp())
            )
        return True

    def get(self, identity: UUID, *, offset: int = 0, limit: int = 50) -> dict:
        with database_session(self.queue.engine) as session:
            instance = session.get(m.Experiment, identity)
            if instance is None:
                raise KeyError("Experiment not found")
            row = row_dict(instance)
            state = func.coalesce(m.ResearchRun.state, "failed")
            row["counts"] = [
                dict(r)
                for r in session.execute(
                    select(state.label("state"), func.count().label("count"))
                    .select_from(m.ExperimentTrial)
                    .outerjoin(m.ResearchRun, m.ResearchRun.id == m.ExperimentTrial.run_id)
                    .where(m.ExperimentTrial.experiment_id == identity)
                    .group_by(state)
                ).mappings()
            ]
            row["trials"] = [
                dict(r)
                for r in session.execute(
                    select(
                        m.ExperimentTrial.ordinal,
                        m.ExperimentTrial.run_id,
                        m.ExperimentTrial.normalized_spec,
                        m.ResearchRun.engine_id,
                        func.coalesce(m.ExperimentTrial.error_code, m.ResearchRun.error_code).label(
                            "error_code"
                        ),
                        state.label("state"),
                        m.ResearchRun.summary,
                    )
                    .outerjoin(m.ResearchRun, m.ResearchRun.id == m.ExperimentTrial.run_id)
                    .where(m.ExperimentTrial.experiment_id == identity)
                    .order_by(m.ExperimentTrial.ordinal)
                    .offset(offset)
                    .limit(limit)
                ).mappings()
            ]
            row["offset"] = offset
            row["limit"] = limit
            row["engine_compatible"] = row["engine_id"] == engine_id()
            complete = (
                row["generated_trials"] == row["total_trials"]
                and sum(count["count"] for count in row["counts"]) == row["total_trials"]
                and not any(count["state"] in ("queued", "running") for count in row["counts"])
            )
            row["status"] = (
                "canceled"
                if row["state"] == "canceled"
                else "completed"
                if complete
                else "active"
                if row["engine_compatible"]
                else "blocked_engine"
            )
            return row

    def cancel(self, identity: UUID) -> dict:
        with database_session(self.queue.engine) as session:
            changed = session.scalar(
                update(m.Experiment)
                .where(m.Experiment.id == identity)
                .values(state="canceled", updated_at=func.clock_timestamp())
                .returning(m.Experiment.id)
            )
            if changed is None:
                raise KeyError("Experiment not found")
            # Run identities are shared across experiments/batches. Detach this
            # experiment instead of canceling work another request may still own.
        return self.get(identity)
