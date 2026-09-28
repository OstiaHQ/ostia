"""`pixi run doctor` prints the environment and diagnoses problems (RFC-0001, Failure handling)."""

import os
import stat
import subprocess
import sys

from .conftest import ROOT

DOCTOR = ROOT / "tools" / "dev" / "doctor.py"
KEYS = [
    "compiler",
    "cuda",
    "cuda_toolkit",
    "architectures",
    "telemetry_level",
    "components",
    "dependencies",
    "ccache",
    "pixi_env",
    "cmake",
    "ninja",
]


def run(env=None, cwd=None):
    return subprocess.run(
        [sys.executable, str(DOCTOR)], capture_output=True, text=True, env=env, cwd=cwd
    )


def test_prints_every_key_and_is_healthy(tmp_path):
    r = run(cwd=tmp_path)  # works from outside the checkout
    for key in KEYS:
        assert f"{key}:" in r.stdout, key
    assert r.returncode == 0, r.stdout + r.stderr


def test_foreign_cmake_on_path_fails(tmp_path):
    fake = tmp_path / "cmake"
    fake.write_text("#!/bin/sh\necho 'cmake version 3.29.0'\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    env = dict(os.environ, PATH=f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    r = run(env=env)
    assert r.returncode == 1
    assert "error: the cmake on PATH is not the pixi environment's" in r.stdout
    assert "fix:" in r.stdout


def test_reports_the_installed_flavour():
    r = run()
    line = next(s for s in r.stdout.splitlines() if s.startswith("installed_flavour:"))
    assert any(level in line for level in ("off", "metrics", "trace", "debug")), line
