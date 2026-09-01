"""CRWALLM command line.

The CLI and the REST API call the same service functions — no duplicated
logic (docs/03_SYSTEM_ARCHITECTURE.md). Commands land in Phase 2.
"""

from __future__ import annotations

import typer

from crwallm import __version__
from crwallm.config import get_settings

app = typer.Typer(
    name="crwallm",
    help="Local AI crawler.",
    no_args_is_help=True,
    add_completion=False,
)


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(f"crwallm {__version__}")


@app.command()
def config() -> None:
    """Show effective settings (secrets redacted)."""
    s = get_settings()
    redacted = "set" if s.api_token else "MISSING"
    lines = [
        f"env           {s.env}",
        f"api           http://{s.api_host}:{s.api_port}",
        f"api_token     {redacted}",
        f"allowed_hosts {', '.join(s.allowed_hosts)}",
        f"database_url  {s.database_url.split('@')[-1]}",
        f"archive_dir   {s.archive_dir}",
    ]
    typer.echo("\n".join(lines))


@app.command()
def serve() -> None:
    """Run the API server."""
    from crwallm.main import main

    main()


if __name__ == "__main__":
    app()
