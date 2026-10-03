"""The commands pixi's task graph used to sequence (RFC-0005 §2.2, ruling B4).

Each command is a list of steps, in the order pixi ran the old task's dependencies (depth
first, each once). Steps run at the repository root, as pixi tasks did, and the first step
that fails ends the command with that step's own exit code (ruling B5).
"""

import os
import subprocess
import sys
from collections.abc import Callable, Iterable
from pathlib import Path

from ostia_dev import errors, paths
from ostia_dev.contract import violation

LEVELS = ["level-off", "level-metrics", "level-trace", "level-debug"]
SANITIZERS = ["asan-ubsan", "tsan"]


def _call(argv: list[str], cwd: Path) -> int:
    return subprocess.call(argv, cwd=cwd)


RUNNER: Callable[[list[str], Path], int] = _call


def env_or_exit(cmd: str) -> str:
    """The pixi environment this command runs in; outside pixi there is no toolchain."""
    env = os.environ.get("PIXI_ENVIRONMENT_NAME")
    if not env:
        raise errors.UsageError(
            violation(
                f"ostia-dev {cmd} must run inside a pixi environment",
                ["PIXI_ENVIRONMENT_NAME: not set"],
                "builds and tests use the locked toolchain of a pixi environment (RFC-0001 §2.2)",
                f"pixi run ostia-dev {cmd}",
                "RFC-0005 §2.2",
            )
        )
    return env


def build_root() -> str:
    return os.environ.get("OSTIA_BUILD_ROOT") or str(paths.build_root(paths.ROOT))


def configure(env: str) -> list[str]:
    """pixi's `_configure` task, which reconfigures only when presets or the lock changed."""
    return ["pixi", "run", "--frozen", "-e", env, "_configure"]


def py(module: str, *args: str) -> list[str]:
    return [sys.executable, "-m", f"ostia_dev.{module}", *args]


def build(env: str, *args: str) -> list[list[str]]:
    return [configure(env), ["cmake", "--build", "--preset", "dev", *args]]


def native_install() -> list[list[str]]:
    root = build_root()
    return [
        ["cmake", "--install", f"{root}/dev", "--prefix", os.environ.get("CONDA_PREFIX", "")],
        [
            "cmake",
            "-E",
            "copy",
            f"{root}/dev/install_manifest.txt",
            f"{root}/native-install-manifest.txt",
        ],
    ]


def py_dev(env: str, *args: str) -> list[list[str]]:
    """Native build and install, then the editables. --build-native builds the given preset
    itself, as running the script directly did."""
    run = [py("dev.py_dev", "--preset", "dev", *args)]
    if "--build-native" in args:
        return run
    return [*build(env), *native_install(), *run]


def all_tests(env: str) -> list[list[str]]:
    return [*build(env), ["ctest", "--preset", "dev"], *py_dev(env), ["pytest"]]


def preset(name: str) -> list[list[str]]:
    return [
        ["cmake", "--preset", name],
        ["cmake", "--build", "--preset", name],
        ["ctest", "--preset", name],
    ]


FUZZ_TARGET = "ostia_fabric_topology_fuzz"


def fuzz(seconds: int, *args: str) -> list[list[str]]:
    """Build the fuzz preset's target and run it for `seconds`. New inputs go to a working
    corpus that persists across runs; the seeds are regenerated from the fixtures at configure."""
    tree = f"{build_root()}/fuzz"
    corpus, crashes = f"{tree}/corpus", f"{tree}/crashes"
    return [
        ["cmake", "--preset", "fuzz"],
        ["cmake", "--build", "--preset", "fuzz", "--target", FUZZ_TARGET],
        ["cmake", "-E", "make_directory", corpus, crashes],
        [
            f"{tree}/fabric/fuzz/{FUZZ_TARGET}",
            f"-max_total_time={seconds}",
            # Well above a slow topo1 search under the sanitizers; a longer input is a finding.
            "-timeout=25",
            f"-artifact_prefix={crashes}/",
            # Rewards inputs that bring a comparison's operands closer, so text integers in the
            # JSON can climb past a threshold the seeds never reach.
            "-use_value_profile=1",
            "-print_final_stats=1",
            *args,
            corpus,
            f"{tree}/fabric/fuzz/seeds",
        ],
    ]


def coverage(env: str) -> list[list[str]]:
    """C++ tests under Clang source-based coverage, then the Python tests under coverage.py.
    Leaves cpp.lcov and python.xml in the coverage tree for the Codecov upload."""
    tree = f"{build_root()}/coverage"
    return [
        ["cmake", "--preset", "coverage"],
        ["cmake", "--build", "--preset", "coverage"],
        # Profiles from an earlier run would be merged into this one.
        ["cmake", "-E", "rm", "-rf", f"{tree}/profiles"],
        ["ctest", "--preset", "coverage"],
        py("dev.coverage", "--build", tree),
        *py_dev(env),
        ["coverage", "erase"],
        ["coverage", "run", "-m", "pytest"],
        ["coverage", "xml", "-o", f"{tree}/python.xml"],
        ["coverage", "report"],
    ]


def graph(env: str) -> list[list[str]]:
    dot = f"{build_root()}/dev/ostia.dot"
    return [
        configure(env),
        ["cmake", "--preset", "dev", f"--graphviz={dot}"],
        py("ci.check_graph", "--dot", dot),
    ]


def macros(env: str) -> list[list[str]]:
    return [configure(env), py("ci.check_telemetry_macros", "--build", f"{build_root()}/dev")]


def dedupe(plan: Iterable[list[str]]) -> list[list[str]]:
    """Each step once, at its first position, as pixi runs a shared dependency once."""
    seen: list[list[str]] = []
    for step in plan:
        if step not in seen:
            seen.append(step)
    return seen


def run(plan: Iterable[list[str]]) -> int:
    for argv in dedupe(plan):
        try:
            code = RUNNER(argv, paths.ROOT)
        except KeyboardInterrupt:
            return errors.INTERRUPTED
        if code:
            return code
    return 0
