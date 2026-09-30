"""The pod supervisor, run for real with stub tools (RFC-0005 §3.2, §4.5).

Each case runs under /bin/sh, and under dash and busybox sh when they are installed
(ubuntu-24.04 CI has dash; on macOS /bin/sh is bash in POSIX mode).
"""

import contextlib
import json
import os
import shutil
import signal
import subprocess
import time
import uuid
from importlib import resources
from pathlib import Path

import pytest

HERE = Path(__file__).parent
SCRIPT = (resources.files("ostia_dev.remote") / "pod" / "supervisor.sh").read_text()


def _shells() -> list[list[str]]:
    shells = [["/bin/sh"]]
    if dash := shutil.which("dash"):
        shells.append([dash])
    if busybox := shutil.which("busybox"):
        shells.append([busybox, "sh"])
    return shells


SHELLS = _shells()


@pytest.fixture(params=SHELLS, ids=[" ".join(s) for s in SHELLS])
def shell(request) -> list[str]:
    return request.param


class Run:
    def __init__(self, shell, work: Path, plan: list[tuple[str, str, str]], *, setsid=None, **env):
        self.work = work
        self.state = work / ".ostia"
        path = [str(HERE / "supervisor" / "stubs")]
        if setsid:
            path.append(str(HERE / "supervisor" / setsid))
        elif not shutil.which("setsid"):
            path.append(str(HERE / "supervisor" / "setsid"))
        full_env = {
            **os.environ,
            "PATH": os.pathsep.join([*path, os.environ["PATH"]]),
            "OSTIA_WORK": str(work),
            "OSTIA_PLAN": "\n".join("\t".join(step) for step in plan),
            "OSTIA_ENV": "default",
            "OSTIA_PRESET": "dev",
            "OSTIA_POLL": "0.2",
            "OSTIA_CODE_WAIT": "3",
            "OSTIA_COLLECT_WINDOW": "3",
            "OSTIA_TIMEOUT": "60",
            "HOME": str(work / "home"),
            **{k: str(v) for k, v in env.items()},
        }
        self.t0 = time.monotonic()
        self.proc = subprocess.Popen(
            [*shell, "-c", SCRIPT, "ostia-supervisor"],
            env=full_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,  # so a test can clean up everything it started
        )

    def ready(self) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "ready").touch()

    def collected(self) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "collected").touch()

    def wait(self, timeout: float = 30) -> int:
        try:
            self.out, _ = self.proc.communicate(timeout=timeout)
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.proc.pid, signal.SIGKILL)
        self.seconds = time.monotonic() - self.t0
        return self.proc.returncode

    @property
    def steps(self) -> dict:
        return json.loads((self.state / "steps.json").read_text())

    def wait_for(self, name: str, timeout: float = 10) -> None:
        end = time.monotonic() + timeout
        while not (self.state / name).exists():
            assert time.monotonic() < end, f"{name} never appeared"
            time.sleep(0.05)


def start(shell, tmp_path, plan, *, ready=True, collected=True, setsid=None, **env) -> Run:
    r = Run(shell, tmp_path / "w", plan, setsid=setsid, **env)
    if ready:
        r.ready()
    if collected:
        r.collected()
    return r


def _alive(marker: str) -> bool:
    out = subprocess.run(["ps", "-A", "-o", "command"], capture_output=True, text=True).stdout
    return any(marker in line for line in out.splitlines())


def test_script_parses(shell):
    assert subprocess.run([*shell, "-n", "-c", SCRIPT]).returncode == 0


def test_no_ready_file_is_exit_3(shell, tmp_path):
    r = start(shell, tmp_path, [("a", "build", "true")], ready=False, OSTIA_CODE_WAIT=1)
    assert r.wait() == 3
    assert r.steps["code_wait"] == "timeout" and r.steps["steps"] == []
    assert r.seconds < 10


def test_per_step_codes_and_valid_json(shell, tmp_path):
    r = start(shell, tmp_path, [("install", "install", "true"), ("build", "build", "echo built")])
    assert r.wait() == 0
    doc = r.steps
    assert doc["state"] == "done" and doc["code_wait"] == "ok" and doc["exit"] == 0
    assert [(s["name"], s["kind"], s["code"], s["result"]) for s in doc["steps"]] == [
        ("install", "install", 0, "ok"),
        ("build", "build", 0, "ok"),
    ]
    assert all(isinstance(s["seconds"], int) for s in doc["steps"])
    assert "built" in (r.state / "log.txt").read_text()


def test_a_failing_step_under_tee_keeps_its_code(shell, tmp_path):
    r = start(shell, tmp_path, [("build", "build", "echo compiler says no; exit 5")])
    assert r.wait() == 5
    (step,) = r.steps["steps"]
    assert (step["code"], step["result"]) == (5, "failed")
    assert "compiler says no" in (r.state / "log.txt").read_text()
    assert "compiler says no" in r.out


def test_a_failing_build_stops_the_plan(shell, tmp_path):
    plan = [("build", "build", "exit 2"), ("command", "command", "echo never")]
    r = start(shell, tmp_path, plan)
    assert r.wait() == 2
    assert [s["name"] for s in r.steps["steps"]] == ["build"]
    assert "never" not in (r.state / "log.txt").read_text().replace("echo never", "")


@pytest.mark.parametrize("kind", ["preflight", "install"])
def test_a_failing_preflight_or_install_is_exit_3(shell, tmp_path, kind):
    r = start(shell, tmp_path, [("x", kind, "exit 1"), ("command", "command", "true")])
    assert r.wait() == 3
    assert [s["name"] for s in r.steps["steps"]] == ["x"]


def test_a_failing_report_step_continues(shell, tmp_path):
    plan = [("aa", "report", "exit 1"), ("command", "command", "true")]
    r = start(shell, tmp_path, plan)
    assert r.wait() == 0
    assert [(s["name"], s["code"]) for s in r.steps["steps"]] == [("aa", 1), ("command", 0)]


def test_the_collected_marker_ends_the_run_early(shell, tmp_path):
    r = start(shell, tmp_path, [("command", "command", "true")], OSTIA_COLLECT_WINDOW=60)
    assert r.wait() == 0
    assert r.seconds < 10


def test_the_collect_window_expires(shell, tmp_path):
    r = start(
        shell, tmp_path, [("command", "command", "exit 1")], collected=False, OSTIA_COLLECT_WINDOW=2
    )
    assert r.wait() == 1
    assert 1 <= r.seconds < 15  # windows are measured in whole seconds
    assert r.steps["state"] == "done"


def test_the_watchdog_stops_a_hung_step(shell, tmp_path):
    marker = f"ostia-hung-{uuid.uuid4().hex[:8]}"
    plan = [
        ("build", "build", f"sh -c 'sleep 60; : {marker}' & wait"),
        ("command", "command", "true"),
    ]
    r = start(shell, tmp_path, plan, OSTIA_TIMEOUT=2)
    assert r.wait() == 3
    doc = r.steps
    assert doc["state"] == "timeout" and doc["exit"] == 3
    assert [(s["name"], s["result"]) for s in doc["steps"]] == [("build", "timeout")]
    assert r.seconds < 20
    time.sleep(0.5)
    assert not _alive(marker), "the step's process group survived the watchdog"


def test_term_is_recorded_as_terminated(shell, tmp_path):
    marker = f"ostia-term-{uuid.uuid4().hex[:8]}"
    r = start(shell, tmp_path, [("build", "build", f"sh -c 'sleep 60; : {marker}' & wait")])
    r.wait_for("pgid")
    time.sleep(0.3)
    os.kill(r.proc.pid, signal.SIGTERM)
    assert r.wait() == 3
    doc = r.steps
    assert doc["state"] == "terminated"
    assert [(s["name"], s["result"]) for s in doc["steps"]] == [("build", "terminated")]
    assert r.seconds < 15
    time.sleep(0.5)
    assert not _alive(marker)


def test_working_directories_and_exported_paths(shell, tmp_path):
    plan = [
        ("configure", "build", 'pwd > "$OSTIA_SOURCE_DIR/.ostia/build-pwd"'),
        (
            "command",
            "command",
            'pwd > "$OSTIA_SOURCE_DIR/.ostia/command-pwd"; echo "$OSTIA_BUILD_DIR"',
        ),
    ]
    r = start(shell, tmp_path, plan)
    assert r.wait() == 0
    work = (tmp_path / "w").resolve()
    assert Path((r.state / "build-pwd").read_text().strip()).resolve() == work
    build = work / "build" / "default" / "dev"
    assert Path((r.state / "command-pwd").read_text().strip()).resolve() == build


def test_no_tests_action_is_error_by_default(shell, tmp_path):
    r = start(shell, tmp_path, [("command", "command", 'echo "nta=$CTEST_NO_TESTS_ACTION"')])
    assert r.wait() == 0
    assert "nta=error" in r.out


def test_a_plan_through_the_pixi_stubs(shell, tmp_path):
    plan = [
        ("install", "install", "pixi install --locked -e default"),
        ("configure", "build", "pixi run --frozen -e default cmake --preset dev"),
        ("build", "build", "pixi run --frozen -e default cmake --build --preset dev"),
        (
            "command",
            "command",
            "pixi run --frozen -e default ctest -L cpu --output-junit junit.xml",
        ),
    ]
    r = start(shell, tmp_path, plan)
    assert r.wait() == 0
    assert [s["code"] for s in r.steps["steps"]] == [0, 0, 0, 0]
    junit = tmp_path / "w" / "build" / "default" / "dev" / "junit.xml"
    assert 'tests="1"' in junit.read_text()


def test_quoted_commands_run_as_written(shell, tmp_path):
    import shlex

    argv = [
        "sh",
        "-c",
        'printf "%s|" "$@" > "$OSTIA_SOURCE_DIR/.ostia/args"',
        "x",
        "a b",
        "$HOME",
        "it's",
    ]
    r = start(shell, tmp_path, [("command", "command", shlex.join(argv))])
    assert r.wait() == 0
    assert (r.state / "args").read_text() == "a b|$HOME|it's|"


def test_a_background_process_left_by_a_step_does_not_hold_the_run(shell, tmp_path):
    marker = f"ostia-stray-{uuid.uuid4().hex[:8]}"
    plan = [("command", "command", f"sh -c 'sleep 30; : {marker}' & echo started")]
    r = start(shell, tmp_path, plan)
    assert r.wait(timeout=20) == 0
    assert r.seconds < 10
    assert [(s["name"], s["code"]) for s in r.steps["steps"]] == [("command", 0)]
    time.sleep(0.5)
    assert not _alive(marker), "the step's leftover process was not stopped"


def test_a_setsid_without_wait_still_gives_real_codes(shell, tmp_path):
    """busybox's setsid applet (which busybox-static's sh prefers) has no -w."""
    marker = f"ostia-nowait-{uuid.uuid4().hex[:8]}"
    plan = [("build", "build", "echo built; exit 0"), ("command", "command", "exit 4"),
            ("never", "command", f"sh -c 'sleep 30; : {marker}'")]  # fmt: skip
    r = start(shell, tmp_path, plan, setsid="setsid-no-wait")
    assert r.wait() == 4
    assert [(s["name"], s["code"]) for s in r.steps["steps"]] == [("build", 0), ("command", 4)]


def test_the_watchdog_works_with_a_setsid_without_wait(shell, tmp_path):
    marker = f"ostia-nowait-hung-{uuid.uuid4().hex[:8]}"
    plan = [("build", "build", f"sh -c 'sleep 60; : {marker}' & wait")]
    r = start(shell, tmp_path, plan, setsid="setsid-no-wait", OSTIA_TIMEOUT=2)
    assert r.wait() == 3
    assert r.steps["state"] == "timeout"
    time.sleep(0.5)
    assert not _alive(marker)
