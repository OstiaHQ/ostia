"""Contract errors on the Python path (RFC-0001 §3.5, Failure handling)."""

import os
import subprocess
import sys

from .conftest import ROOT


def test_pip_install_without_native_prefix_explains_fix(tmp_path):
    empty = tmp_path / "prefix"
    empty.mkdir()
    env = dict(os.environ, CONDA_PREFIX=str(empty), CMAKE_PREFIX_PATH=str(empty))
    r = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--no-build-isolation", "--no-deps",
         "--target", str(tmp_path / "t"), f"-Cbuild-dir={tmp_path / 'b'}",
         f"-Ccmake.define.CMAKE_PREFIX_PATH={empty}",
         # Do not let CMake find the environment's own install through PATH.
         "-Ccmake.define.CMAKE_FIND_USE_SYSTEM_ENVIRONMENT_PATH=OFF",
         "-Ccmake.define.CMAKE_FIND_USE_CMAKE_SYSTEM_PATH=OFF",
         str(ROOT / "telemetry" / "python")],
        capture_output=True, text=True, env=env,
    )
    assert r.returncode != 0
    assert "fix: run pixi run py-dev" in r.stdout + r.stderr
