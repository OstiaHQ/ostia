"""Shared fixtures for the cross-component Python tests (RFC-0001 §3.5)."""

import os
import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def run_py(tmp_path):
    """Run Python code in a fresh interpreter, outside the checkout, without PYTHONPATH."""

    def run(code: str) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        return subprocess.run(
            [sys.executable, "-c", code], cwd=tmp_path, env=env, capture_output=True, text=True
        )

    return run


@pytest.fixture
def site_arch() -> str:
    return sysconfig.get_paths()["platlib"]


@pytest.fixture
def conda_lib() -> Path:
    return Path(os.environ["CONDA_PREFIX"], "lib").resolve()
