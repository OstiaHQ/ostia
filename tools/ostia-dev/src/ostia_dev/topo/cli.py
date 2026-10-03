"""topo show and topo golden (RFC-0003 §9, RFC-0005 §2.2)."""

import re
import subprocess
from pathlib import Path
from typing import Annotated

import typer

from ostia_dev import errors, paths
from ostia_dev.contract import violation
from ostia_dev.dev import steps

topo_app = typer.Typer(help="Topology fixtures (RFC-0003).", no_args_is_help=True)

FIXTURES = paths.ROOT / "fabric/tests/fixtures/topology"
INPUTS = ("hwloc.xml", "pair.json")
BUILD_TOPO = ["cmake", "--build", "--preset", "dev", "--target", "ostia-topo"]


def _binary() -> Path:
    return Path(steps.build_root()) / "dev/fabric/tools/topo/ostia-topo"


def _model(fixture: Path) -> str:
    return subprocess.run(
        [str(_binary()), "model", str(fixture)], check=True, capture_output=True, text=True
    ).stdout


def _fixtures() -> list[Path]:
    found = {p.parent for name in INPUTS for p in FIXTURES.glob(f"*/*/{name}")}
    return sorted(found)


def _is_fixture(d: Path) -> bool:
    return d.is_dir() and any((d / name).is_file() for name in INPUTS)


def _resolve(arg: Path) -> Path:
    return arg if arg.is_absolute() or arg.exists() else paths.ROOT / arg


def _bad_fixture(arg: Path) -> errors.UsageError:
    available = [str(p.relative_to(FIXTURES)) for p in _fixtures()] if FIXTURES.is_dir() else []
    return errors.UsageError(
        violation(
            f"{arg} is not a topology fixture",
            [f"available: {a}" for a in available],
            "a fixture is a directory holding hwloc.xml or pair.json (RFC-0003 §9)",
            f"pass one of the directories under {FIXTURES.relative_to(paths.ROOT)}",
            "RFC-0003 §9",
        )
    )


@topo_app.command()
def show(
    fixture: Annotated[Path, typer.Argument(help="A fixture directory.")],
) -> None:
    """Print a fixture's discovered topology."""
    fixture = _resolve(fixture)
    if not _is_fixture(fixture):
        raise _bad_fixture(fixture)
    env = steps.env_or_exit("topo show")
    plan = [steps.configure(env), BUILD_TOPO, [str(_binary()), "show", str(fixture)]]
    raise typer.Exit(steps.run(plan))


@topo_app.command()
def golden(
    fixtures: Annotated[
        list[Path] | None, typer.Argument(help="Fixtures to check or update; default all.")
    ] = None,
    update: Annotated[
        bool, typer.Option("--update", help="Rewrite expected.json; review it like code.")
    ] = False,
) -> None:
    """Run the golden-model tests, or regenerate the goldens with --update."""
    chosen = [_resolve(f) for f in fixtures] if fixtures else _fixtures()
    for f in chosen:
        if not _is_fixture(f):
            raise _bad_fixture(f)
    env = steps.env_or_exit("topo golden")
    if not update:
        pattern = r"^fabric\.topo\.golden\."
        if fixtures:
            names = "|".join(re.escape(f.name) for f in chosen)
            pattern += f"({names})$"
        code = steps.run(
            [steps.configure(env), ["cmake", "--build", "--preset", "dev"]]
            + [["ctest", "--preset", "dev", "-R", pattern]]
        )
        raise typer.Exit(code)
    code = steps.run([steps.configure(env), BUILD_TOPO])
    if code:
        raise typer.Exit(code)
    changed, same = [], []
    for f in chosen:
        target = f / "expected.json"
        new = _model(f)
        if target.is_file() and target.read_text() == new:
            same.append(target)
            continue
        target.write_text(new)
        changed.append(target)
    for t in changed:
        typer.echo(f"updated {t.relative_to(FIXTURES)}")
    for t in same:
        typer.echo(f"unchanged {t.relative_to(FIXTURES)}")
    typer.echo(f"{len(changed)} changed, {len(same)} unchanged")


def register(app: typer.Typer) -> None:
    app.add_typer(topo_app, name="topo")
