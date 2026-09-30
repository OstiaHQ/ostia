"""The ostia-dev entry point (RFC-0005 §1.1): `ostia-dev = "ostia_dev.cli:app"`."""

from importlib.metadata import version

import typer

from ostia_dev.remote.cli import app as remote_app

app = typer.Typer(help="Ostia's contributor CLI (RFC-0005).", no_args_is_help=True)
app.add_typer(remote_app, name="remote")


def _version(value: bool) -> None:
    if value:
        typer.echo(f"ostia-dev {version('ostia-dev')}")
        raise typer.Exit()


@app.callback()
def main(
    show_version: bool = typer.Option(
        False, "--version", callback=_version, is_eager=True, help="Print the version."
    ),
) -> None:
    """Ostia's contributor CLI (RFC-0005)."""
