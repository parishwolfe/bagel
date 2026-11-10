import logging
import signal
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Event
from typing import Annotated
from uuid import UUID

import typer
from psycopg import Error, OperationalError
from pydantic import TypeAdapter
from sqlalchemy.exc import SQLAlchemyError

from api.cli import serve as serve_api
from market.backtesting.engine import CashOrSplitEvent
from market.data.domain import TimeRange, parse_boundary
from market.data.observability import configure_logging
from market.research.artifacts import Artifacts, canonical_json
from market.research.config import ResearchSettings
from market.research.datasets import export_local, synthetic_fixture
from market.research.queue import BatchSpec, Queue
from market.research.worker import work_once

app = typer.Typer(no_args_is_help=True, help="Offline strategy research and backtesting.")
log = logging.getLogger(__name__)


@contextmanager
def context() -> Iterator[tuple[ResearchSettings, Queue, Artifacts]]:
    try:
        settings = ResearchSettings()
        with Queue(settings.dsn()) as queue:
            yield settings, queue, Artifacts(settings.artifacts)
    except (ValueError, KeyError) as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(1) from None
    except (Error, SQLAlchemyError, OSError) as error:
        typer.echo(f"Local research operation failed ({type(error).__name__}).", err=True)
        raise typer.Exit(1) from None


def echo(value: object) -> None:
    typer.echo(canonical_json(value).decode())


@app.command()
def fixture(sessions: Annotated[int, typer.Option(min=1, max=100)] = 3):
    """Register deterministic synthetic equity bars for an offline smoke test."""
    with context() as (_, queue, artifacts):
        manifest = synthetic_fixture(artifacts, sessions)
        echo({"dataset_id": queue.register(manifest), "rows": manifest.rows})


@app.command()
def export(
    series: Annotated[list[int], typer.Option(help="A local series ID. Repeat for each instrument.")],
    start: Annotated[str, typer.Option()],
    end: Annotated[str, typer.Option()],
    actions: Annotated[Path | None, typer.Option(exists=True, dir_okay=False)] = None,
):
    """Freeze regular-session bars already present in the local database."""
    with context() as (settings, queue, artifacts):
        events = (
            TypeAdapter(tuple[CashOrSplitEvent, ...]).validate_json(actions.read_bytes()) if actions else ()
        )
        manifest = export_local(
            settings.dsn(),
            artifacts,
            series,
            TimeRange(parse_boundary(start), parse_boundary(end)),
            events,
        )
        echo({"dataset_id": queue.register(manifest), "rows": manifest.rows})


@app.command()
def submit(file: Annotated[Path, typer.Argument(exists=True, dir_okay=False)]):
    """Submit a JSON batch definition and return its durable ID."""
    with context() as (_, queue, _):
        echo({"batch_id": queue.enqueue(BatchSpec.model_validate_json(file.read_bytes()))})


@app.command()
def batch(batch_id: UUID):
    """Show each run, its status, and available summary metrics."""
    with context() as (_, queue, _):
        echo(queue.batch(batch_id))


@app.command()
def work(drain: Annotated[bool, typer.Option(help="Exit when no runs are claimable.")] = False):
    """Process queued runs without a provider connection."""
    configure_logging("INFO")
    stop = Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    with context() as (_, queue, artifacts):
        while not stop.is_set():
            try:
                if work_once(queue, artifacts, stop.is_set):
                    continue
                if drain:
                    return
            except (OperationalError, SQLAlchemyError) as error:
                if drain:
                    raise
                log.warning("research_db_retry", extra={"error_type": type(error).__name__})
            stop.wait(1)


@app.command()
def serve(
    host: Annotated[str, typer.Option()] = "127.0.0.1",
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8001,
):
    """Run the local API with its integrated offline research worker."""
    serve_api(host=host, port=port)
