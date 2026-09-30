"""The `ostia-dev remote` command group (RFC-0005 §3.1) and `remote container` (§5)."""

import os
import subprocess
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

from ostia_dev import config, proc
from ostia_dev.contract import violation
from ostia_dev.errors import UsageError
from ostia_dev.remote.container import ContainerBackend
from ostia_dev.remote.core import RunSpec, drive
from ostia_dev.remote.profiles import parse_duration

app = typer.Typer(
    help="Run the build and tests somewhere else: a container or a cluster (RFC-0005).",
    no_args_is_help=True,
)


class Engine(StrEnum):
    podman = "podman"
    docker = "docker"


def repo_root() -> Path:
    """The checkout the run uploads: pixi's project root, else git's top level of the cwd."""
    if root := os.environ.get("PIXI_PROJECT_ROOT"):
        return Path(root)
    r = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if r.returncode != 0:
        raise UsageError(
            violation(
                "not inside an Ostia checkout",
                [f"cwd: {Path.cwd()}"],
                "remote runs upload the checkout they are started from",
                "cd into your Ostia checkout, or run it with pixi run ostia-dev ...",
                "RFC-0005 §4.10",
            )
        )
    return Path(r.stdout.strip())


def _check_flags(preset: str | None, suite: str | None, timeout: str | None) -> None:
    if preset and suite:
        raise UsageError(
            violation(
                f"--preset {preset} and --suite {suite} were both given",
                [],
                "a suite names its own preset (RFC-0005 §3.3)",
                "drop --preset",
                "RFC-0005 §3.1",
            )
        )
    if timeout:
        parse_duration(timeout)


@app.command()
def container(
    command: Annotated[
        list[str] | None,
        typer.Argument(metavar="[-- COMMAND]", help="Run this after the build instead of ctest."),
    ] = None,
    env: Annotated[
        list[str] | None,
        typer.Option(
            "--env",
            help="pixi environment; repeat to run once per environment. "
            "Default: cuda-12 on GPU profiles, else default.",
        ),
    ] = None,
    preset: Annotated[str | None, typer.Option(help="CMake preset. Default: dev.")] = None,
    suite: Annotated[
        str | None, typer.Option(help="A named suite (gpu, sanitizer, bench-smoke, ...).")
    ] = None,
    no_build: Annotated[bool, typer.Option("--no-build", help="Skip configure and build.")] = False,
    no_test: Annotated[bool, typer.Option("--no-test", help="Stop after the build.")] = False,
    timeout: Annotated[
        str | None, typer.Option(help="Limit for the pipeline in the container (60m).")
    ] = None,
    ref: Annotated[
        str | None, typer.Option(help="Run a pushed commit <sha> or pull request pr/<n>.")
    ] = None,
    env_var: Annotated[
        list[str] | None, typer.Option("--env-var", help="Pass KEY=VALUE; repeatable.")
    ] = None,
    allow_secret: Annotated[
        bool, typer.Option("--allow-secret", help="Allow secret-looking --env-var keys.")
    ] = False,
    results: Annotated[
        Path | None, typer.Option(help="Where results land. Default: build/remote/.")
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", help="Answer yes to confirmations.")] = False,
    verbose: Annotated[
        bool, typer.Option("-v", "--verbose", help="Print every external command.")
    ] = False,
    profile: Annotated[
        str,
        typer.Option(
            help="Node profile; with --gpus a GPU profile such as l4 (its compute capability)."
        ),
    ] = "cpu",
    gpus: Annotated[
        bool, typer.Option("--gpus", help="Pass the host's NVIDIA GPUs through.")
    ] = False,
    engine: Annotated[
        Engine | None, typer.Option(help="Container engine. Default: podman, else docker.")
    ] = None,
) -> None:
    """Run the pipeline in a local podman or docker container (RFC-0005 §5).

    On a Mac the container is linux/arm64 and compile-only: `--env cuda-12 --env cuda-13
    --preset release --no-test` replaces check-cuda. On a Linux host with an NVIDIA GPU,
    `--gpus --profile l4` runs the GPU suites.
    """
    if verbose:
        proc.set_verbose(True)
    _check_flags(preset, suite, timeout)
    cfg = config.load()
    repo = repo_root()
    kind = cfg.profiles.get(profile, {}).get("kind", "gpu")
    spec = RunSpec(
        backend="container",
        profile=profile,
        envs=env or (["cuda-12"] if kind in ("gpu", "rdma") else ["default"]),
        results=results or repo / "build" / "remote",
        preset=preset,
        suite=suite,
        command=command or None,
        no_build=no_build,
        no_test=no_test,
        timeout=timeout,
        ref=ref,
        env_vars=env_var or [],
        allow_secret=allow_secret,
        yes=yes,
        verbose=verbose,
    )
    backend = ContainerBackend(engine.value if engine else None, gpus=gpus)
    raise typer.Exit(drive(backend, spec, cfg=cfg, repo=repo))
