import json
import logging
import time
from collections.abc import Callable
from dataclasses import asdict

from psycopg import OperationalError
from sqlalchemy.exc import OperationalError as PoolOperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeout

from market.backtesting.engine import BacktestSpec, run_backtest
from market.portfolio.ledger import Fill
from market.research.artifacts import Artifacts, canonical_json
from market.research.datasets import read_bars
from market.research.queue import LEASE_SECONDS, Queue

log = logging.getLogger(__name__)


class InterruptedRun(Exception):
    """The process is shutting down or the database has revoked its claim."""


def work_once(queue: Queue, artifacts: Artifacts, stopping: Callable[[], bool] = lambda: False):
    claim = queue.claim()
    if claim is None:
        return False
    renewed = time.monotonic()

    def checkpoint(force: bool = False) -> None:
        nonlocal renewed
        if stopping():
            raise InterruptedRun
        if force or time.monotonic() - renewed >= LEASE_SECONDS / 4:
            if not queue.renew(claim):
                raise InterruptedRun
            renewed = time.monotonic()

    try:
        manifest = queue.dataset(claim.dataset_id)
        spec = BacktestSpec.model_validate(claim.spec)
        if not spec.instruments:
            spec = spec.model_copy(
                update={"instruments": tuple(series.instrument for series in manifest.series)}
            )
        with artifacts.temporary() as fill_path:
            with fill_path.open("wb") as output:

                def record_fill(fill: Fill) -> None:
                    output.write(canonical_json(asdict(fill)) + b"\n")

                results = run_backtest(
                    read_bars(artifacts, manifest), spec, manifest.actions, checkpoint, record_fill
                )
            checkpoint(force=True)
            results["fill_log_sha256"] = artifacts.publish(fill_path)
            results["fill_log_format"] = "bagel-fills-jsonl-v1"
        results["summary"]["dataset_availability"] = manifest.availability
        checkpoint(force=True)
        digest = artifacts.write_json(results)
        if queue.finish(claim, result=digest, summary=json.loads(canonical_json(results["summary"]))):
            log.info("research_run_complete", extra={"run_id": str(claim.run_id)})
    except InterruptedRun:
        log.info("research_run_interrupted", extra={"run_id": str(claim.run_id)})
    except (OperationalError, PoolOperationalError, PoolTimeout):
        raise
    except Exception as error:
        kind = type(error).__name__
        queue.finish(claim, error=kind)
        log.error("research_run_failed", extra={"run_id": str(claim.run_id), "error_type": kind})
    return True
