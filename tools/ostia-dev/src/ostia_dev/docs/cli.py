"""docs index and docs figures (RFC-0005 §2.2)."""

import shutil
from importlib.resources import files
from typing import Annotated

import typer

from ostia_dev import errors, passthrough, paths
from ostia_dev.contract import violation
from ostia_dev.dev import steps

docs_app = typer.Typer(help="The RFC/ADR index and the PRD's figures.", no_args_is_help=True)
passthrough.command(docs_app, "index", "docs.gen_index", "Regenerate the RFC/ADR index; --check.")

FIGURES = "docs/product/figures"


@docs_app.command()
def figures(
    src: Annotated[str, typer.Argument(help="Figure sources (*.jsx).")] = f"{FIGURES}/src",
    out: Annotated[str, typer.Argument(help="Where the SVGs go.")] = FIGURES,
) -> None:
    """Render the hand-drawn figures to theme-aware SVGs (needs bun)."""
    if not shutil.which("bun"):
        raise errors.UsageError(
            violation(
                "ostia-dev docs figures needs bun, which is not on PATH",
                [],
                "figure sources are JSX modules that bun renders (docs/README.md)",
                "curl -fsSL https://bun.sh/install | bash",
                "RFC-0005 §2.2",
            )
        )
    script = str(files("ostia_dev.docs") / "render-figures.js")
    args = [str(paths.ROOT / p) for p in (src, out)]
    raise typer.Exit(steps.run([["bun", script, *args]]))


def register(app: typer.Typer) -> None:
    app.add_typer(docs_app, name="docs")
