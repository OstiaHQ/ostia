"""lint, fmt and the check group (RFC-0005 §2.2): CI's checks, one vocabulary."""

import typer

from ostia_dev import passthrough
from ostia_dev.dev import steps
from ostia_dev.passthrough import FORWARD

check_app = typer.Typer(
    help="Everything CI requires (lint, graph, macros, tests), or one check by name.",
    invoke_without_command=True,
)


@check_app.callback()
def check(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand:
        return
    env = steps.env_or_exit("check")
    plan = [steps.py("ci.lint", "lint"), *steps.graph(env), *steps.macros(env)]
    raise typer.Exit(steps.run([*plan, *steps.all_tests(env)]))


FORWARDED = [
    ("layering", "ci.check_layering", "Include layering of each component (RFC-0001 §3.3)."),
    ("cpm-pins", "ci.check_cpm_pins", "CPM dependencies pinned to a tag and SHA (RFC-0001 §2.4)."),
    ("exports", "ci.check_exports", "A library exports exactly its ABI list (RFC-0001 §5)."),
    ("comments", "ci.check_comments", "Comments that break ADR-0015 (files, or --hook)."),
    ("docs-as-test", "ci.docs_as_test", "Run a guide's marked command blocks verbatim."),
]
for verb, mod, text in FORWARDED:
    passthrough.command(check_app, verb, mod, text)


@check_app.command(context_settings=FORWARD, add_help_option=False)
def graph(ctx: typer.Context) -> None:
    """The resolved link graph against the layering table (reconfigures); --dot FILE checks
    a graph you already have."""
    if ctx.args:
        raise typer.Exit(passthrough.call("ci.check_graph", passthrough.forwarded(ctx.args)))
    raise typer.Exit(steps.run(steps.graph(steps.env_or_exit("check graph"))))


@check_app.command(context_settings=FORWARD, add_help_option=False)
def macros(ctx: typer.Context) -> None:
    """Telemetry macro arguments must not change state (libclang); --public checks only
    public headers."""
    if ctx.args:
        raise typer.Exit(
            passthrough.call("ci.check_telemetry_macros", passthrough.forwarded(ctx.args))
        )
    raise typer.Exit(steps.run(steps.macros(steps.env_or_exit("check macros"))))


@check_app.command(context_settings=FORWARD, add_help_option=False)
def tidy(ctx: typer.Context) -> None:
    """clang-tidy over the compile database, or over FILES (slow)."""
    env = steps.env_or_exit("check tidy")
    tidy = [
        "run-clang-tidy",
        "-quiet",
        "-p",
        f"{steps.build_root()}/dev",
        *passthrough.forwarded(ctx.args),
    ]
    raise typer.Exit(steps.run([steps.configure(env), tidy]))


def register(app: typer.Typer) -> None:
    passthrough.command(
        app,
        "lint",
        "ci.lint",
        "Fast checks: format, ruff, gersemi, layering, CPM pins, comments, docs index.",
        ("lint",),
    )
    passthrough.command(app, "fmt", "ci.lint", "Apply clang-format, ruff and gersemi.", ("fmt",))
    app.add_typer(check_app, name="check")
