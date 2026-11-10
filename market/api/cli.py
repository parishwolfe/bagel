"""Single local backend entry point."""

from typing import Annotated

import typer
import uvicorn
from psycopg import Error
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from market.api.research import create_app
from market.data.observability import configure_logging

app = typer.Typer(help="Bagel API with integrated research and optional market-data ingestion.")


@app.command()
def serve(
    host: Annotated[str, typer.Option()] = "127.0.0.1",
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8001,
    ingestion_enabled: Annotated[
        bool,
        typer.Option(
            envvar="BAGEL_INGESTION_ENABLED",
            help="Enable provider access only after explicit authorization.",
        ),
    ] = False,
):
    """Run the API and its background modules in one backend process."""
    configure_logging("INFO")
    try:
        application = create_app(ingestion_enabled=ingestion_enabled)
    except ValidationError as error:
        fields = sorted({".".join(map(str, item["loc"])) for item in error.errors()})
        typer.echo("Invalid backend configuration: " + ", ".join(fields), err=True)
        raise typer.Exit(1) from None
    except (Error, SQLAlchemyError, OSError) as error:
        typer.echo(f"Backend configuration unavailable ({type(error).__name__}).", err=True)
        raise typer.Exit(1) from None
    uvicorn.run(application, host=host, port=port)
