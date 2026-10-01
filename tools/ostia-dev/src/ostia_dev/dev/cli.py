"""build, test, py-dev, clean, doctor and hooks (RFC-0005 §2.2)."""

from typing import Annotated

import typer

from ostia_dev import errors, passthrough
from ostia_dev.contract import violation
from ostia_dev.dev import steps
from ostia_dev.passthrough import FORWARD

test_app = typer.Typer(
    help="Build and run the C++, CMake and Python tests (or one part, preset or label).",
    invoke_without_command=True,
)


def _usage(problem: str, rule: str, fix: str) -> errors.UsageError:
    return errors.UsageError(violation(problem, [], rule, fix, "RFC-0005 §2.2"))


@test_app.callback()
def test(
    ctx: typer.Context,
    preset: Annotated[
        str | None, typer.Option(help="Configure, build and test this preset, e.g. level-off.")
    ] = None,
    levels: Annotated[bool, typer.Option(help="All four telemetry levels (RFC-0001 §5).")] = False,
    sanitize: Annotated[
        str | None, typer.Option(help="Build and test with asan-ubsan or tsan (Clang).")
    ] = None,
    label: Annotated[
        str | None, typer.Option("-L", "--label", help="Only ctest tests with this label.")
    ] = None,
) -> None:
    chosen = [
        f
        for f, on in (
            ("--preset", preset),
            ("--levels", levels),
            ("--sanitize", sanitize),
            ("-L", label),
        )
        if on
    ]
    if len(chosen) > 1 or (chosen and ctx.invoked_subcommand):
        raise _usage(
            f"{' and '.join(chosen + ([ctx.invoked_subcommand] if ctx.invoked_subcommand else []))}"
            " select different test runs",
            "ostia-dev test runs one selection at a time",
            "give one of --preset, --levels, --sanitize, -L, cpp, py or rebuild",
        )
    if ctx.invoked_subcommand:
        return
    if sanitize is not None and sanitize not in steps.SANITIZERS:
        raise _usage(
            f"unknown sanitizer {sanitize}",
            f"the sanitizer presets are {', '.join(steps.SANITIZERS)}",
            "ostia-dev test --sanitize asan-ubsan (or tsan)",
        )
    env = steps.env_or_exit("test")
    if preset or sanitize:
        raise typer.Exit(steps.run(steps.preset(preset or sanitize)))
    if levels:
        raise typer.Exit(steps.run(p for lv in steps.LEVELS for p in steps.preset(lv)))
    if label:
        raise typer.Exit(steps.run([*steps.build(env), ["ctest", "--preset", "dev", "-L", label]]))
    raise typer.Exit(steps.run(steps.all_tests(env)))


@test_app.command(context_settings=FORWARD, add_help_option=False)
def cpp(ctx: typer.Context) -> None:
    """C++ and CMake tests (ctest); extra arguments go to ctest, e.g. -R Result."""
    env = steps.env_or_exit("test cpp")
    raise typer.Exit(
        steps.run(
            [*steps.build(env), ["ctest", "--preset", "dev", *passthrough.forwarded(ctx.args)]]
        )
    )


@test_app.command(context_settings=FORWARD, add_help_option=False)
def py(ctx: typer.Context) -> None:
    """Python tests (pytest) after py-dev; extra arguments go to pytest."""
    env = steps.env_or_exit("test py")
    raise typer.Exit(steps.run([*steps.py_dev(env), ["pytest", *passthrough.forwarded(ctx.args)]]))


@test_app.command()
def rebuild() -> None:
    """Slow tests: a native change is visible from Python after a rebuild."""
    env = steps.env_or_exit("test rebuild")
    raise typer.Exit(steps.run([*steps.py_dev(env), ["pytest", "-m", "slow", "tests/python"]]))


def register(app: typer.Typer) -> None:
    @app.command(context_settings=FORWARD, add_help_option=False)
    def build(ctx: typer.Context) -> None:
        """Configure (when needed) and build the dev preset; extra arguments go to cmake."""
        raise typer.Exit(
            steps.run(steps.build(steps.env_or_exit("build"), *passthrough.forwarded(ctx.args)))
        )

    app.add_typer(test_app, name="test")

    @app.command("py-dev", context_settings=FORWARD, add_help_option=False)
    def py_dev(ctx: typer.Context) -> None:
        """Build and install native code, then the Python editables (RFC-0001 §3.5)."""
        raise typer.Exit(
            steps.run(steps.py_dev(steps.env_or_exit("py-dev"), *passthrough.forwarded(ctx.args)))
        )

    passthrough.command(app, "clean", "dev.clean", "Remove build output and what py-dev installed.")
    passthrough.command(app, "doctor", "dev.doctor", "Print the environment and diagnose problems.")

    @app.command()
    def hooks() -> None:
        """Install the git pre-commit hook, which runs ostia-dev lint."""
        raise typer.Exit(steps.run([["pre-commit", "install"]]))
