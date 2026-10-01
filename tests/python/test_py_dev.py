"""`pixi run ostia-dev py-dev` is idempotent (RFC-0001 §3.5)."""

import subprocess
import sys

import pytest

from .test_namespace import _load

pytestmark = pytest.mark.slow


def _py_dev(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "ostia_dev.dev.py_dev", *args],
        capture_output=True,
        text=True,
    )


def test_py_dev_idempotent(run_py):
    first = _py_dev()
    assert first.returncode == 0, first.stdout + first.stderr
    second = _py_dev()
    assert second.returncode == 0, second.stdout + second.stderr
    assert "telemetry: up to date" in second.stdout
    assert "fabric: up to date" in second.stdout
    data = _load(run_py)
    assert len(data["tele"]) == 1 and data["levels"][0] == data["levels"][1]
