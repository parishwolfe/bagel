import json
import logging
import time
from collections.abc import Callable

from psycopg import OperationalError
from psycopg_pool import PoolTimeout

from market.backtesting.engine import BacktestSpec, run_backtest
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
        results = run_backtest(read_bars(artifacts, manifest), spec, manifest.actions, checkpoint)
        results["summary"]["dataset_availability"] = manifest.availability
        checkpoint(force=True)
        digest = artifacts.write_json(results)
        if queue.finish(
            claim, result=digest, summary=json.loads(canonical_json(results["summary"]))
        ):
            log.info("research_run_complete", extra={"run_id": str(claim.run_id)})
    except InterruptedRun:
        log.info("research_run_interrupted", extra={"run_id": str(claim.run_id)})
    except (OperationalError, PoolTimeout):
        raise
    except Exception as error:
        kind = type(error).__name__
        queue.finish(claim, error=kind)
        log.error("research_run_failed", extra={"run_id": str(claim.run_id), "error_type": kind})
    return True
