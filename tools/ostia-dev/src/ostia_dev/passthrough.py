"""Commands that hand their arguments to a moved tool's argparse `main` (RFC-0005 §2.3,
ruling B1), so parsing, help and errors stay the tool's own; only the program name changes.
"""

import importlib
import sys

import typer

FORWARD = {
    "allow_extra_args": True,
    "ignore_unknown_options": True,
    "help_option_names": [],
}


def forwarded(args: list[str]) -> list[str]:
    """The extra arguments as typed: click drops the first `--`, but the tools split their
    own arguments from their program's on it (`bench run … -- --mem cuda`)."""
    argv = sys.argv[1:]
    if "--" not in argv:
        return list(args)
    tail = argv[argv.index("--") + 1 :]
    if tail and args[len(args) - len(tail) :] != tail:
        return list(args)
    return [*args[: len(args) - len(tail)], "--", *tail]


def call(module: str, argv: list[str]) -> int:
    """Run `ostia_dev.<module>.main(argv)`; the module is imported only now, so `--help` on
    the CLI never loads PyYAML or libclang."""
    return importlib.import_module(f"ostia_dev.{module}").main(argv)


def command(app: typer.Typer, name: str, module: str, help: str, prefix: tuple = ()) -> None:
    """Register `name` on `app` as a pass-through to `module`, with `prefix` before the
    user's arguments."""

    @app.command(name, help=help, context_settings=FORWARD, add_help_option=False)
    def _forward(ctx: typer.Context) -> None:
        raise typer.Exit(call(module, [*prefix, *forwarded(ctx.args)]))
