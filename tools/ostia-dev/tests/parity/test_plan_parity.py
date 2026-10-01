"""Plan rows: each old pixi task, expanded the way pixi runs it (dependencies depth-first,
each once), against the commands its ostia-dev replacement runs (RFC-0005 §2.2, ruling B4).

A wrapped tool's exit code passes through unchanged (ruling B5), so a failure at any step
stops the plan there with that step's code, as `pixi run` did.
"""

import shlex
import sys
import tomllib
from pathlib import Path

import pytest
from fakes.steps import RecordingRunner
from renames import SCRIPTS, TASKS, module

pytestmark = pytest.mark.parity

ENV = {
    "PIXI_ENVIRONMENT_NAME": "default",
    "OSTIA_BUILD_ROOT": "/fake/build/default",
    "CONDA_PREFIX": "/fake/prefix",
}
PY = sys.executable
CONFIGURE = ["pixi", "run", "--frozen", "-e", "default", "_configure"]

# (old task, its extra arguments). check-cuda is a replacement, not a rename (ruling B9).
CASES = [
    ("build", []),
    ("test", []),
    ("test-cpp", ["-R", "Result"]),
    ("test-py", ["-k", "telemetry"]),
    ("test-rebuild", []),
    ("test-multiprocess", []),
    ("test-preset", ["level-off"]),
    ("test-levels", []),
    ("sanitize-asan", []),
    ("sanitize-tsan", []),
    ("install-native", []),
    ("py-dev", []),
    ("lint", []),
    ("fmt", []),
    ("check", []),
    ("check-graph", []),
    ("check-macros", []),
    ("tidy", []),
    ("doctor", []),
    ("clean", []),
    ("hooks", []),
    ("docs-index", []),
    ("docs-as-test", []),
    ("bench", ["run", "--bench", "/b/noop"]),
    ("compare", ["--baseline", "a.jsonl", "--candidate", "b.jsonl"]),
]
# The plan task whose commands make a row pass; LANDED lists the tasks done so far.
TASK_OF = {"docs-index": 5, "bench": 6, "compare": 6}
LANDED = {4, 5, 6}


def _command(part: str) -> list[str]:
    for var, value in ENV.items():
        part = part.replace(f"${var}", value)
    argv = shlex.split(part)
    if argv[:1] == ["python"] and argv[1] in SCRIPTS:
        return [PY, "-m", module(argv[1]), *argv[2:]]
    return argv


def expand(tasks: dict, name: str, args: list[str], extra: list[str], seen: set) -> list:
    key = (name, tuple(args))
    if key in seen:
        return []
    seen.add(key)
    if name == "_configure":
        return [CONFIGURE]
    task = tasks[name]
    plan = []
    for dep in task.get("depends-on", []):
        dep_name, dep_args = (dep, []) if isinstance(dep, str) else (dep["task"], dep["args"])
        plan += expand(tasks, dep_name, dep_args, [], seen)
    cmd = task.get("cmd")
    if cmd:
        for arg_name, value in zip(task.get("args", []), args, strict=False):
            cmd = cmd.replace("{{ " + arg_name + " }}", value)
        parts = [_command(p) for p in cmd.split(" && ")]
        parts[-1] += extra
        plan += parts
    return plan


def old_plan(base: Path, task: str, extra: list[str]) -> list[list[str]]:
    tasks = tomllib.loads((base / "pixi.toml").read_text())["tasks"]
    declared = tasks[task].get("args", [])
    return expand(tasks, task, extra[: len(declared)], extra[len(declared) :], set())


def new_plan(monkeypatch, argv: list[str], runner: RecordingRunner) -> int:
    from ostia_dev import passthrough
    from ostia_dev.cli import app
    from ostia_dev.dev import steps
    from typer.testing import CliRunner

    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(steps, "RUNNER", runner)
    monkeypatch.setattr(
        passthrough, "call", lambda mod, args: runner([PY, "-m", f"ostia_dev.{mod}", *args], Path())
    )
    return CliRunner().invoke(app, argv).exit_code


def _new_argv(task: str, extra: list[str]) -> list[str]:
    return [*TASKS[task], *extra]


def _params():
    params = []
    for t, e in CASES:
        task = TASK_OF.get(t, 4)
        marks = [] if task in LANDED else [pytest.mark.xfail(strict=True, reason=f"Task {task}")]
        params.append(pytest.param(t, e, id=t, marks=marks))
    return params


@pytest.mark.parametrize(("task", "extra"), _params())
def test_plan(task, extra, base_tree, monkeypatch):
    expected = old_plan(base_tree, task, extra)
    runner = RecordingRunner()
    assert new_plan(monkeypatch, _new_argv(task, extra), runner) == 0
    if task == "install-native":
        # Folded into py-dev (ruling B3): the old task is the start of the new plan.
        assert runner.calls[: len(expected)] == expected
    else:
        assert runner.calls == expected


@pytest.mark.parametrize(("task", "extra"), _params())
def test_plan_stops_at_a_failing_step_with_its_code(task, extra, base_tree, monkeypatch):
    expected = old_plan(base_tree, task, extra)
    if task == "install-native":
        pytest.skip("covered by py-dev")
    for i in range(len(expected)):
        runner = RecordingRunner(fail_at=i, code=8)
        assert new_plan(monkeypatch, _new_argv(task, extra), runner) == 8, (task, i)
        assert runner.calls == expected[: i + 1], (task, i)
