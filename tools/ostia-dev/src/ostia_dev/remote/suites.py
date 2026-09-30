"""Suites and commands become supervisor step plans (RFC-0005 §3.2, §3.3).

A plan is a list of Steps. Every step's argv is complete: the install step is
`pixi install --locked -e <env>`, and every later step runs through
`pixi run --frozen -e <env>`, so the pod never re-solves pixi.lock. encode() writes the
plan for the supervisor's OSTIA_PLAN variable, one `name<TAB>kind<TAB>command` line per
step, where the command is shlex-quoted once and run by `sh -c` in the pod.
"""

import re
import shlex
from dataclasses import dataclass, field

from ostia_dev.config import Config
from ostia_dev.contract import violation
from ostia_dev.errors import UsageError
from ostia_dev.remote.profiles import Profile, parallelism

WORK = "/w"  # the work volume in the pod or container (§4.5)
KINDS = ("preflight", "install", "build", "command", "report")
_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


@dataclass(frozen=True)
class Step:
    name: str
    kind: str
    argv: tuple[str, ...]

    def __post_init__(self) -> None:
        # The supervisor writes names into steps.json verbatim, so they must need no escaping
        if not _NAME.match(self.name):
            raise ValueError(f"step name {self.name!r} must match {_NAME.pattern}")
        if self.kind not in KINDS:
            raise ValueError(f"step kind {self.kind!r} is not one of {', '.join(KINDS)}")


@dataclass
class Plan:
    steps: list[Step]
    env: str
    preset: str
    build_dir: str  # the command's working directory in the pod, OSTIA_BUILD_DIR
    suite: str | None = None
    expect_summary: str | None = None  # a line the configure summary must contain
    build_jobs: int = 1
    test_jobs: int = 1
    notes: list[str] = field(default_factory=list)


def _pixi(env: str, argv: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    return ("pixi", "run", "--frozen", "-e", env, *argv)


def gpu_preflight(profile: Profile) -> list[Step]:
    """The GPU checks that run before anything else on GPU profiles (§4.9), without pixi."""
    cc = profile.compute_capability or ""
    fail = 'echo "  see: RFC-0005 §4.9"; exit 1'
    fingerprint = (
        'command -v nvidia-smi >/dev/null || { echo "error: nvidia-smi not found on PATH=$PATH"; '
        'echo "  fix: set the profile\'s env (PATH, LD_LIBRARY_PATH) for this provider"; '
        f"{fail}; }}; "
        "sh tools/ci/gpu_preflight.sh > .ostia/fingerprint.txt; rc=$?; "
        "cat .ostia/fingerprint.txt; exit $rc"
    )
    capability = (
        "cc=$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader | head -n 1); "
        f'if [ "$cc" != "{cc}" ]; then '
        f'echo "error: the GPU has compute capability ${{cc:-unknown}}, profile {profile.name} '
        f'expects {cc}"; '
        'echo "  rule: a run must land on the node type its profile names"; '
        f'echo "  fix: check the node_selector of profile {profile.name}"; {fail}; fi; '
        'echo "compute capability $cc: ok"'
    )
    libcuda = (
        "if ldconfig -p 2>/dev/null | grep -q 'libcuda[.]so[.]1'; then "
        'echo "libcuda.so.1: found by ldconfig"; exit 0; fi; '
        'IFS=:; for d in ${LD_LIBRARY_PATH:-}; do if [ -e "$d/libcuda.so.1" ]; then '
        'echo "libcuda.so.1: $d"; exit 0; fi; done; '
        'echo "error: libcuda.so.1 not found by ldconfig or in '
        'LD_LIBRARY_PATH=${LD_LIBRARY_PATH:-}"; '
        'echo "  fix: set the profile\'s env (LD_LIBRARY_PATH) for this provider"; '
        f"{fail}"
    )
    return [
        Step("gpu-preflight", "preflight", ("sh", "-c", fingerprint)),
        Step("compute-capability", "preflight", ("sh", "-c", capability)),
        Step("libcuda", "preflight", ("sh", "-c", libcuda)),
    ]


def _arch_flags(profile: Profile, preset: str) -> list[str]:
    if profile.is_gpu and preset != "dev" and profile.cc:
        return [f"-DCMAKE_CUDA_ARCHITECTURES={profile.cc}-real"]
    return []


def _with_junit(argv: list[str]) -> list[str]:
    if argv and argv[0] == "ctest" and not any(a.startswith("--output-junit") for a in argv):
        return [*argv, "--output-junit", "junit.xml"]
    return argv


def _unknown(what: str, name: str, known) -> UsageError:
    return UsageError(
        violation(
            f"unknown {what} {name!r}",
            [f"{what}s: {', '.join(sorted(known))}"],
            "suites are named step lists in profiles.toml",
            "pick one of them",
            "RFC-0005 §3.3",
        )
    )


def build_plan(
    cfg: Config,
    profile: Profile,
    env: str,
    *,
    suite: str | None = None,
    command: list[str] | None = None,
    preset: str | None = None,
    no_build: bool = False,
    no_test: bool = False,
    run_id: str = "",
) -> Plan:
    if suite and command:
        raise UsageError(
            violation(
                "--suite and a command after -- were both given",
                [f"suite: {suite}", f"command: {shlex.join(command)}"],
                "a run executes one suite or one command",
                "drop one of them",
                "RFC-0005 §3.1",
            )
        )
    build_jobs, test_jobs = parallelism(profile, env)
    steps = gpu_preflight(profile) if profile.is_gpu else []
    steps.append(Step("install", "install", ("pixi", "install", "--locked", "-e", env)))
    expect = None
    if suite:
        if suite not in cfg.suites:
            raise _unknown("suite", suite, cfg.suites)
        spec = cfg.suites[suite]
        preset = spec["preset"]
        build_dir = f"{WORK}/build/{env}/{preset}"
        values = {
            "build_dir": build_dir,
            "source_dir": WORK,
            "env": env,
            "cc": profile.cc,
            "cc_dot": profile.compute_capability or "",
            "profile": profile.name,
            "run_id": run_id,
        }
        if spec.get("expect_summary") and profile.is_gpu:
            expect = spec["expect_summary"].format_map(values)
        for s in spec["steps"]:
            if (no_build and s["kind"] == "build") or (
                no_test and s["kind"] in ("command", "report")
            ):
                continue
            argv = [a.format_map(values) for a in s["run"]]
            steps.append(Step(s["name"], s["kind"], _pixi(env, argv)))
    else:
        preset = preset or "dev"
        build_dir = f"{WORK}/build/{env}/{preset}"
        if not no_build:
            configure = ["cmake", "--preset", preset, *_arch_flags(profile, preset)]
            steps.append(Step("configure", "build", _pixi(env, configure)))
            steps.append(
                Step("build", "build", _pixi(env, ["cmake", "--build", "--preset", preset]))
            )
        if not no_test:
            argv = command or [
                "ctest", "--test-dir", build_dir, "--output-on-failure", "-j", str(test_jobs),
            ]  # fmt: skip
            steps.append(Step("command", "command", _pixi(env, _with_junit(list(argv)))))
    return Plan(
        steps=steps,
        env=env,
        preset=preset,
        build_dir=build_dir,
        suite=suite,
        expect_summary=expect,
        build_jobs=build_jobs,
        test_jobs=test_jobs,
    )


def encode(steps: list[Step]) -> str:
    """The OSTIA_PLAN text: `name<TAB>kind<TAB>command` per line."""
    lines = []
    for s in steps:
        for a in s.argv:
            if "\n" in a or "\t" in a:
                raise UsageError(
                    violation(
                        "a command argument contains a newline or a tab",
                        [f"step: {s.name}", f"argument: {a!r}"],
                        "the step plan is one line per step, so arguments can't span lines",
                        "put the command in a script in the working tree and run the script",
                        "RFC-0005 §4.5",
                    )
                )
        lines.append(f"{s.name}\t{s.kind}\t{shlex.join(s.argv)}")
    return "\n".join(lines)
