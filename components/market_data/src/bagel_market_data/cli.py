"""Operator CLI. Dry runs never contact providers or databases."""

import asyncio
import json
from contextlib import contextmanager
from datetime import timedelta

import psycopg
import typer
from pydantic import ValidationError

from .capacity import load_watchlist
from .config import Settings
from .domain import TimeRange
from .historical import ingest_history
from .observability import configure_logging
from .providers.alpaca import AlpacaREST, ProviderError
from .runtime import run_worker
from .storage import PostgresStore
from .watchlist import CapacityExceeded, HistoryDoesNotFit

app = typer.Typer(no_args_is_help=True, help="Bagel market-data ingestion and diagnostics.")


@contextmanager
def errors():
    try:
        yield
    except typer.Exit:
        raise
    except ValidationError as exc:
        fields = sorted({".".join(map(str, e["loc"])) or "configuration" for e in exc.errors()})
        typer.echo("Invalid configuration: " + ", ".join(fields), err=True)
        raise typer.Exit(1) from None
    except (ValueError, ProviderError, CapacityExceeded, HistoryDoesNotFit) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None
    except (psycopg.Error, OSError, RuntimeError) as exc:
        # libpq and network exceptions may contain DSNs. Never echo their raw messages.
        typer.echo(
            f"Operation failed: {type(exc).__name__}. Check configuration and service health.",
            err=True,
        )
        raise typer.Exit(1) from None


def settings_for(symbols: str | None) -> Settings:
    settings = Settings()
    if symbols:
        settings = Settings.model_validate({**settings.model_dump(), "symbols": symbols})
    configure_logging(settings.log_level)
    return settings


def historical_command(
    start: str | None, end: str | None, symbols: str | None, dry_run: bool, force: bool
) -> None:
    with errors():
        settings = settings_for(symbols)
        if force and not start and not settings.history_start:
            start = (
                (settings.eligible_end() - timedelta(days=settings.reconcile_days))
                .date()
                .isoformat()
            )
        window = settings.history_window(start, end)
        if dry_run:
            typer.echo(
                json.dumps(
                    {
                        "mode": "reconcile" if force else "backfill",
                        "symbols": settings.symbols,
                        "feed": settings.feed,
                        "adjustment": "raw",
                        "interval": "1Min",
                        "start_inclusive": window.start.isoformat(),
                        "end_exclusive": window.end.isoformat(),
                        "day_chunks": sum(1 for _ in window.chunks()),
                        "max_history_years": settings.max_history_years,
                        "database_budget_mb": settings.database_budget_mb,
                        "fallback_years": [5, 3, 2, 1],
                    },
                    indent=2,
                )
            )
            return
        settings.require_credentials()
        assert settings.database_url is not None
        provider = AlpacaREST(settings)
        try:
            with PostgresStore(settings.database_url.get_secret_value()) as store:
                if force:
                    result = ingest_history(provider, store, settings, window, force=True)
                else:
                    target = settings.model_copy(
                        update={
                            "history_start": start or settings.history_start,
                            "history_end": end or settings.history_end,
                        }
                    )
                    snapshot = store.configure_watchlist(target)
                    result = load_watchlist(provider, store, target, snapshot)
                typer.echo(json.dumps(result))
        finally:
            provider.close()


@app.command()
def backfill(
    start: str | None = typer.Option(None, help="Inclusive NY date or timestamp with offset."),
    end: str | None = typer.Option(None, help="Exclusive NY date or timestamp with offset."),
    symbols: str | None = typer.Option(None, help="Comma-separated symbols."),
    dry_run: bool = typer.Option(False, help="Print the load plan without connecting."),
):
    """Load history; committed ranges are skipped when resuming."""
    historical_command(start, end, symbols, dry_run, False)


@app.command()
def reconcile(
    start: str | None = typer.Option(None, help="Defaults to the recent reconciliation window."),
    end: str | None = typer.Option(None),
    symbols: str | None = typer.Option(None),
    dry_run: bool = typer.Option(False),
):
    """Re-query completed ranges to apply provider corrections."""
    historical_command(start, end, symbols, dry_run, True)


@app.command("check-config")
def check_config(
    online: bool = typer.Option(False, help="Also check API access and database schema."),
):
    """Report missing credential names; never print their values."""
    with errors():
        settings = settings_for(None)
        missing = settings.missing_credentials()
        typer.echo(
            json.dumps(
                {
                    "symbols": settings.symbols,
                    "feed": settings.feed,
                    "missing": missing,
                    "online": online,
                }
            )
        )
        if missing:
            raise typer.Exit(1)
        if online:
            assert settings.database_url is not None
            provider = AlpacaREST(settings)
            try:
                for symbol in settings.symbols:
                    provider.instrument(symbol)
                end = settings.eligible_end()
                provider.calendar((end - timedelta(days=7)).date(), end.date())
                list(provider.pages(settings.symbols, TimeRange(end - timedelta(minutes=1), end)))
                with PostgresStore(settings.database_url.get_secret_value()) as store:
                    store.ping()
            finally:
                provider.close()
            typer.echo("API access and database schema checks passed; no data was written.")


@app.command()
def run(symbols: str | None = typer.Option(None)):
    """Run the singleton live worker with continuous historical repair."""
    with errors():
        restart = True
        while restart:
            settings = settings_for(symbols)
            settings.require_credentials()
            assert settings.database_url is not None
            provider = AlpacaREST(settings)
            try:
                with PostgresStore(settings.database_url.get_secret_value()) as store:
                    restart = asyncio.run(
                        run_worker(
                            settings, store, provider, reload_settings=lambda: settings_for(symbols)
                        )
                    )
            finally:
                provider.close()


@app.command()
def watchlist(
    symbols: str | None = typer.Option(None, help="Save a new comma-separated watchlist."),
):
    """Show/change the database watchlist; a running worker detects its new revision."""
    with errors():
        settings = settings_for(symbols)
        if settings.database_url is None:
            raise ValueError("Set DATABASE_URL to read or update the watchlist")
        with PostgresStore(settings.database_url.get_secret_value()) as store:
            snapshot = (
                store.set_watchlist(settings)
                if symbols
                else store.watchlist("alpaca:" + settings.feed)
            )
            typer.echo(
                json.dumps(
                    {
                        "symbols": snapshot.symbols,
                        "revision": snapshot.revision,
                        "loaded_revision": snapshot.loaded_revision,
                        "history_years": snapshot.attempt_years,
                        "status": snapshot.status,
                    }
                )
            )


if __name__ == "__main__":
    app()
