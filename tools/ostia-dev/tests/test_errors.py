"""Exit codes and the entry point's error mapping (RFC-0005 §1.4, §3.5)."""

import os
import shutil
import signal
import subprocess
import sys
import time

import pytest
from ostia_dev import errors

EXE = shutil.which("ostia-dev")


@pytest.mark.parametrize(
    ("cls", "code"),
    [
        (errors.CheckFailed, 1),
        (errors.UsageError, 2),
        (errors.InfraError, 3),
        (errors.TeardownUnverified, 4),
    ],
)
def test_one_exit_code_per_error_class(cls, code):
    e = cls("error: x")
    assert isinstance(e, errors.OstiaError)
    assert e.code == code and e.message == "error: x"


def test_ostia_error_takes_an_explicit_code():
    assert errors.OstiaError("m", 3).code == 3


def test_exit_code_constants():
    assert (errors.OK, errors.FAILED, errors.USAGE, errors.INFRA) == (0, 1, 2, 3)
    assert (errors.TEARDOWN, errors.INTERRUPTED) == (4, 130)


def _run(*args: str, **kw) -> subprocess.CompletedProcess:
    assert EXE, "ostia-dev is not installed in this environment"
    return subprocess.run([EXE, *args], capture_output=True, text=True, timeout=60, **kw)


def test_installed_usage_error_exits_2():
    r = _run("--no-such-flag")
    assert r.returncode == 2
    assert "Traceback" not in r.stderr


def test_installed_ostia_error_exits_with_its_code_and_message():
    r = _run("_selftest", "--code", "3")
    assert r.returncode == 3
    assert r.stderr.startswith("error: selftest error with exit code 3")
    assert "Traceback" not in r.stderr


def test_installed_unexpected_exception_is_a_one_line_bug_report():
    r = _run("_selftest", "--crash")
    assert r.returncode == 1
    assert "Traceback" not in r.stderr
    assert "RuntimeError" in r.stderr and "-v" in r.stderr


def test_installed_unexpected_exception_shows_the_traceback_with_v():
    r = _run("-v", "_selftest", "--crash")
    assert r.returncode == 1
    assert "Traceback" in r.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_installed_sigint_exits_130_without_traceback():
    assert EXE
    p = subprocess.Popen(
        [EXE, "_selftest", "--sleep", "30"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert p.stdout is not None
    assert p.stdout.readline().strip() == "sleeping"  # the handler is installed by now
    os.kill(p.pid, signal.SIGINT)
    t0 = time.monotonic()
    _, err = p.communicate(timeout=20)
    assert p.returncode == 130
    assert "Traceback" not in err
    assert time.monotonic() - t0 < 10
