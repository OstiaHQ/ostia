"""The ostia-dev entry point (RFC-0005 §1.1). `app` is an OstiaApp, so the installed
command itself maps errors to exit codes (§1.4).
"""

import sys
import time
import traceback
from importlib.metadata import version
from typing import Annotated

import typer

from ostia_dev import errors, proc
from ostia_dev.contract import violation
from ostia_dev.remote.cli import app as remote_app


def _verbose_requested() -> bool:
    if proc.verbose():
        return True
    argv = sys.argv[1:]
    argv = argv[: argv.index("--")] if "--" in argv else argv
    return "-v" in argv or "--verbose" in argv


class OstiaApp(typer.Typer):
    def __call__(self, *args, **kwargs):
        try:
            return super().__call__(*args, **kwargs)
        except errors.OstiaError as e:
            print(e.message, file=sys.stderr)
            sys.exit(e.code)
        except KeyboardInterrupt:
            sys.exit(errors.INTERRUPTED)
        except Exception as e:
            if _verbose_requested():
                traceback.print_exc()
            print(
                violation(
                    f"ostia-dev failed unexpectedly: {type(e).__name__}: {e}",
                    [],
                    "this is a bug in ostia-dev, not in what it checked",
                    "rerun with -v for the traceback, and report it with that output",
                    "RFC-0005 §1.3",
                ),
                file=sys.stderr,
            )
            sys.exit(errors.FAILED)


app = OstiaApp(
    help="Ostia's contributor CLI (RFC-0005).",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
app.add_typer(remote_app, name="remote")


def _version(value: bool) -> None:
    if value:
        typer.echo(f"ostia-dev {version('ostia-dev')}")
        raise typer.Exit()


@app.callback()
def main(
    show_version: Annotated[
        bool,
        typer.Option("--version", callback=_version, is_eager=True, help="Print the version."),
    ] = False,
    verbose: Annotated[
        bool, typer.Option("-v", "--verbose", help="Print every external command it runs.")
    ] = False,
) -> None:
    """Ostia's contributor CLI (RFC-0005)."""
    if verbose:
        proc.set_verbose(True)


@app.command("_selftest", hidden=True)
def _selftest(
    code: Annotated[int, typer.Option(help="Raise an OstiaError with this exit code.")] = 0,
    crash: Annotated[bool, typer.Option(help="Raise an unexpected exception.")] = False,
    sleep: Annotated[float, typer.Option(help="Sleep this many seconds (SIGINT test).")] = 0.0,
) -> None:
    """Test hook for the entry point's error mapping (tests/test_errors.py)."""
    if code:
        raise errors.OstiaError(f"error: selftest error with exit code {code}", code)
    if crash:
        raise RuntimeError("selftest crash")
    if sleep:
        print("sleeping", flush=True)
        time.sleep(sleep)
