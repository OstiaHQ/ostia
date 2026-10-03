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


def _binary() -> Path:
    return Path(steps.build_root()) / "dev/fabric/tools/topo/ostia-topo"


def _model(fixture: Path) -> str:
    try:
        return subprocess.run(
            [str(_binary()), "model", str(fixture)],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout
    except subprocess.CalledProcessError as e:
        raise errors.InfraError(
            violation(
                f"ostia-topo could not model {fixture.name}",
                [f"exit code: {e.returncode}", *(e.stderr or "").strip().splitlines()],
                "a fixture must be readable and replayable for its golden to be regenerated",
                f"pixi run ostia-dev topo show {fixture.name}",
                "RFC-0003 §9",
            ),
            e.returncode,
        ) from e


def _fixtures() -> list[Path]:
    found = {p.parent for name in INPUTS for p in FIXTURES.glob(f"*/*/{name}")}
    return sorted(found)


def _is_fixture(d: Path) -> bool:
    return d.is_dir() and any((d / name).is_file() for name in INPUTS)


def _bad_fixture(arg: Path, why: str | None = None, matches: list[Path] | None = None):
    names = matches or _fixtures()
    return errors.UsageError(
        violation(
            why or f"{arg} is not a topology fixture",
            [f"available: {p.relative_to(FIXTURES).as_posix()}" for p in names],
            "a fixture is a directory holding hwloc.xml or pair.json (RFC-0003 §9)",
            "pass a fixture directory, <group>/<case>, or a unique <case> name",
            "RFC-0003 §9",
        )
    )


def _resolve(arg: Path) -> Path:
    """An absolute fixture directory from a path, `<group>/<case>`, or a unique case name."""
    for candidate in (arg, paths.ROOT / arg, FIXTURES / arg):
        if _is_fixture(candidate):
            return candidate.resolve()
    if len(arg.parts) == 1:
        matches = [f for f in _fixtures() if f.name == arg.name]
        if len(matches) == 1:
            return matches[0].resolve()
        if len(matches) > 1:
            raise _bad_fixture(arg, f"{arg} matches more than one fixture", matches)
    raise _bad_fixture(arg)


@topo_app.command()
def show(
    fixture: Annotated[Path, typer.Argument(help="A fixture directory.")],
) -> None:
    """Print a fixture's discovered topology."""
    fixture = _resolve(fixture)
    env = steps.env_or_exit("topo show")
    plan = [*steps.build(env, "--target", "ostia-topo"), [str(_binary()), "show", str(fixture)]]
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
    env = steps.env_or_exit("topo golden")
    if not update:
        pattern = r"^fabric\.topo\.golden\."
        if fixtures:
            names = "|".join(re.escape(f.name) for f in chosen)
            pattern += f"({names})$"
        code = steps.run([*steps.build(env), ["ctest", "--preset", "dev", "-R", pattern]])
        raise typer.Exit(code)
    code = steps.run(steps.build(env, "--target", "ostia-topo"))
    if code:
        raise typer.Exit(code)
    models = {f: _model(f) for f in chosen}
    changed, same = [], []
    for f, new in models.items():
        target = f / "expected.json"
        if target.is_file() and target.read_text(encoding="utf-8") == new:
            same.append(target)
        else:
            target.write_text(new, encoding="utf-8")
            changed.append(target)
    for t in changed:
        typer.echo(f"updated {t.relative_to(FIXTURES)}")
    for t in same:
        typer.echo(f"unchanged {t.relative_to(FIXTURES)}")
    typer.echo(f"{len(changed)} changed, {len(same)} unchanged")


def register(app: typer.Typer) -> None:
    app.add_typer(topo_app, name="topo")
