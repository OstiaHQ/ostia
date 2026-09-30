"""The `ostia-dev remote` command group (RFC-0005 §3.1): `container` (§5) and `k8s` (§4)."""

import os
import subprocess
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from typer.core import TyperGroup

from ostia_dev import config, proc
from ostia_dev.contract import violation
from ostia_dev.errors import UsageError
from ostia_dev.remote.container import ContainerBackend
from ostia_dev.remote.core import RunSpec, drive
from ostia_dev.remote.k8s import kubectl, preflight
from ostia_dev.remote.k8s.backend import K8sBackend
from ostia_dev.remote.k8s.kube import Kube
from ostia_dev.remote.profiles import parse_duration

app = typer.Typer(
    help="Run the build and tests somewhere else: a container or a cluster (RFC-0005).",
    no_args_is_help=True,
)

Command = Annotated[
    list[str] | None,
    typer.Argument(metavar="[-- COMMAND]", help="Run this after the build instead of ctest."),
]
Env = Annotated[
    list[str] | None,
    typer.Option(
        "--env",
        help="pixi environment; repeat to run once per environment. "
        "Default: cuda-12 on GPU profiles, else default.",
    ),
]
Preset = Annotated[str | None, typer.Option(help="CMake preset. Default: dev.")]
Suite = Annotated[str | None, typer.Option(help="A named suite (gpu, sanitizer, bench-smoke, …).")]
NoBuild = Annotated[bool, typer.Option("--no-build", help="Skip configure and build.")]
NoTest = Annotated[bool, typer.Option("--no-test", help="Stop after the build.")]
Timeout = Annotated[str | None, typer.Option(help="Limit for the pipeline itself (60m).")]
Ref = Annotated[str | None, typer.Option(help="Run a pushed commit <sha> or pull request pr/<n>.")]
EnvVar = Annotated[list[str] | None, typer.Option("--env-var", help="Pass KEY=VALUE; repeatable.")]
AllowSecret = Annotated[
    bool, typer.Option("--allow-secret", help="Allow secret-looking --env-var keys.")
]
Results = Annotated[Path | None, typer.Option(help="Where results land. Default: build/remote/.")]
Yes = Annotated[bool, typer.Option("--yes", help="Answer yes to confirmations.")]
Verbose = Annotated[bool, typer.Option("-v", "--verbose", help="Print every external command.")]


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


def _check_flags(preset: str | None, suite: str | None, *durations: str | None) -> None:
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
    for d in durations:
        if d:
            parse_duration(d)


def _default_envs(cfg, profile: str, env: list[str] | None) -> list[str]:
    kind = cfg.profiles.get(profile, {}).get("kind", "gpu")
    return env or (["cuda-12"] if kind in ("gpu", "rdma") else ["default"])


@app.command()
def container(
    command: Command = None,
    env: Env = None,
    preset: Preset = None,
    suite: Suite = None,
    no_build: NoBuild = False,
    no_test: NoTest = False,
    timeout: Timeout = None,
    ref: Ref = None,
    env_var: EnvVar = None,
    allow_secret: AllowSecret = False,
    results: Results = None,
    yes: Yes = False,
    verbose: Verbose = False,
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
    spec = RunSpec(
        backend="container",
        profile=profile,
        envs=_default_envs(cfg, profile, env),
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


KEEP = "--keep-on-failure"


class K8sGroup(TyperGroup):
    """`remote k8s [run flags] -- <command>` is the run; `remote k8s <subcommand>` the rest.

    click has no optional-value options, so a bare --keep-on-failure becomes =30m.
    """

    def parse_args(self, ctx, args):
        cut = args.index("--") if "--" in args else len(args)
        head = [f"{KEEP}=30m" if a == KEEP else a for a in args[:cut]]
        args = head + args[cut:]
        if args and args[0] not in self.commands and args[0] not in ("--help", "-h"):
            args = ["run", *args]
        return super().parse_args(ctx, args)


k8s_app = typer.Typer(
    cls=K8sGroup,
    no_args_is_help=True,
    help="Run on a Kubernetes cluster (RFC-0005 §4): `ostia-dev remote k8s \\[run flags] -- "
    "<command>` (flags: `remote k8s run --help`), or one of the commands below.",
)
app.add_typer(k8s_app, name="k8s")


def run_k8s(spec: RunSpec, cfg, repo: Path) -> int:
    target, kube = preflight.resolve(spec, cfg, kube_factory=Kube)
    spec.provider = target.provider
    return drive(K8sBackend(cfg=cfg, target=target, kube=kube), spec, cfg=cfg, repo=repo)


@k8s_app.command("run", hidden=True)
def k8s_run(
    command: Command = None,
    context: Annotated[str | None, typer.Option(help="kube context (required).")] = None,
    namespace: Annotated[
        str | None, typer.Option(help="Namespace. Default: the context's configured one.")
    ] = None,
    profile: Annotated[
        str | None, typer.Option(help="Node profile, e.g. l4, cpu (required).")
    ] = None,
    env: Env = None,
    preset: Preset = None,
    suite: Suite = None,
    no_build: NoBuild = False,
    no_test: NoTest = False,
    timeout: Timeout = None,
    ref: Ref = None,
    env_var: EnvVar = None,
    allow_secret: AllowSecret = False,
    results: Results = None,
    yes: Yes = False,
    verbose: Verbose = False,
    schedule_timeout: Annotated[
        str | None, typer.Option(help="How long to wait for the pod to start (20m).")
    ] = None,
    cache: Annotated[
        bool, typer.Option("--cache", help="Mount the download cache (§4.7).")
    ] = False,
    keep_on_failure: Annotated[
        str | None,
        typer.Option(KEEP, help="Keep a failed pod for debugging, 30m or =N (§4.8)."),
    ] = None,
    allow_unguarded: Annotated[
        bool, typer.Option("--allow-unguarded", help="Run in a namespace without guardrails.")
    ] = False,
    kubectl_path: Annotated[
        str | None, typer.Option("--kubectl", help="Another kubectl binary.")
    ] = None,
) -> None:
    """Run the pipeline in a pod on a Kubernetes cluster (RFC-0005 §4)."""
    if verbose:
        proc.set_verbose(True)
    if not profile:
        raise UsageError(
            violation(
                "remote k8s needs --profile",
                [],
                "a run names its node type (§4.4)",
                "pass --profile <name>; ostia-dev remote k8s profiles lists them",
                "RFC-0005 §4.4",
            )
        )
    _check_flags(preset, suite, timeout, schedule_timeout, keep_on_failure)
    cfg = config.load()
    repo = repo_root()
    spec = RunSpec(
        backend="k8s",
        profile=profile,
        envs=_default_envs(cfg, profile, env),
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
        extra={
            "context": context,
            "namespace": namespace,
            "schedule_timeout": schedule_timeout,
            "cache": cache,
            "allow_unguarded": allow_unguarded,
            "kubectl": kubectl_path,
            "keep": keep_on_failure,
        },
    )
    raise typer.Exit(run_k8s(spec, cfg, repo))


@k8s_app.command("kubectl")
def k8s_kubectl(
    update_shas: Annotated[
        bool, typer.Option("--update-shas", help="Refill the sha256 table for the pin.")
    ] = False,
) -> None:
    """Print the pinned kubectl's path and version, downloading it if needed."""
    if update_shas:
        for key, sha in kubectl.update_shas().items():
            typer.echo(f"{key} {sha}")
        return
    typer.echo(f"{kubectl.ensure()} {kubectl.VERSION}")
