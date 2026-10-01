"""A remote k8s run end to end on kind (RFC-0005 Testing: kind rows; R3)."""

import json
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest
from kindlib import CONFIG, cli, cli_argv, kubectl, objects, run_args, wait_for

pytestmark = pytest.mark.kind


def _results(stdout: str) -> Path:
    line = next(line for line in stdout.splitlines() if line.startswith("results: "))
    return Path(line.removeprefix("results: "))


def test_cpu_run_end_to_end():
    r = cli(*run_args("--no-build", "--", "python", "-c", "print(1)"))
    assert r.returncode == 0, r.stdout + r.stderr
    results = _results(r.stdout)
    summary = json.loads((results / "summary.json").read_text())
    assert (summary["exit_code"], summary["teardown"], summary["backend"]) == (0, "verified", "k8s")
    assert (results / "log.txt").exists() and summary["node"]
    assert objects(f"ostia.dev/run-id={summary['run_id']}") == []


def test_a_failing_command_exits_1():
    r = cli(*run_args("--no-build", "--cache", "--", "false"))
    assert r.returncode == 1, r.stdout + r.stderr


@pytest.mark.skipif(not os.environ.get("OSTIA_KIND_NIGHTLY"), reason="nightly only (~20 min)")
def test_cpu_suite():
    r = cli(*run_args("--suite", "cpu", "--cache"), timeout=2700)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr


def test_sigkill_of_the_cli_then_the_run_ends_by_itself():
    p = subprocess.Popen(
        cli_argv(*run_args("--no-build", "--cache", "--timeout", "5m", "--", "true")),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env={**os.environ, "OSTIA_CONFIG": str(CONFIG)},
    )

    def running():
        return any(o["kind"] == "Pod" and o["status"].get("phase") == "Running" for o in objects())

    wait_for(running, 300, "the run's pod to be Running")
    p.send_signal(signal.SIGKILL)
    p.wait()
    # code_wait 60 + timeout 300 + collect 30 + ttl 20 + 60 s of slack
    wait_for(lambda: objects() == [], 470, "the Job and pods to be gone", every=5)


def test_deleting_a_running_job_is_fast():
    p = subprocess.Popen(
        cli_argv(*run_args("--no-build", "--cache", "--", "sleep", "300")),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env={**os.environ, "OSTIA_CONFIG": str(CONFIG)},
    )
    try:

        def sleeping():
            logs = kubectl(
                "logs", "-l", "ostia.dev/managed=true", "--tail", "50", check=False
            ).stdout
            return "step command" in logs

        wait_for(sleeping, 600, "the command step to start")
        (job,) = [o for o in objects() if o["kind"] == "Job"]
        t0 = time.monotonic()
        kubectl("delete", "job", job["metadata"]["name"], "--wait=true", timeout=60)
        wait_for(lambda: objects() == [], 30, "the pod to go", every=0.5)
        assert time.monotonic() - t0 < 10, "the supervisor did not handle TERM (R3)"
    finally:
        p.kill()
        p.wait()
