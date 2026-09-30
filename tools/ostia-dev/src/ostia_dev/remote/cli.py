"""The `ostia-dev remote` command group (RFC-0005 §3.1)."""

import typer

app = typer.Typer(help="Run the build and tests somewhere else: a container or a cluster.")
