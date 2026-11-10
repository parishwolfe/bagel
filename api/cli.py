"""Compatibility shortcut; Compose and development can invoke Uvicorn directly."""

from pathlib import Path
from typing import Annotated

import typer
import uvicorn

app = typer.Typer(help="Bagel control API. Equivalent to uvicorn api.main:app.")


@app.command()
def serve(
    host: Annotated[str, typer.Option()] = "127.0.0.1",
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8001,
    reload: Annotated[bool, typer.Option(help="Reload Python source changes during development.")] = True,
) -> None:
    """Run the local API; provider access remains disabled by default."""
    root = Path(__file__).resolve().parent.parent
    uvicorn.run(
        "api.main:app",
        host=host,
        port=port,
        reload=reload,
        reload_dirs=[str(root / "api"), str(root / "market")] if reload else None,
    )
