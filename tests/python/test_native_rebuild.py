"""A native change is visible from Python after the documented rebuild (RFC-0001 §3.5).

Runs on a `git archive` copy, so the contributor's checkout is never edited.
"""

import subprocess
import sys

import pytest

from .conftest import ROOT

pytestmark = pytest.mark.slow

LEVEL = "constexpr int kBuildLevel = 1;"
CODE = (
    "import ostia.telemetry, ostia.fabric; "
    "print(ostia.telemetry.build_level(), ostia.fabric.telemetry_build_level())"
)


def test_native_change_visible_after_rebuild(tmp_path, run_py):
    src = tmp_path / "src"
    src.mkdir()
    archive = subprocess.run(
        ["git", "-C", str(ROOT), "archive", "HEAD"], capture_output=True, check=True
    )
    subprocess.run(["tar", "-x", "-C", str(src)], input=archive.stdout, check=True)
    cpp = src / "telemetry" / "src" / "telemetry.cpp"
    assert LEVEL in cpp.read_text()
    cpp.write_text(cpp.read_text().replace(LEVEL, "constexpr int kBuildLevel = 2;"))
    py_dev = [sys.executable, str(src / "tools" / "dev" / "py_dev.py")]
    try:
        r = subprocess.run(
            [
                *py_dev,
                "--source-root",
                str(src),
                "--build-root",
                str(tmp_path / "build"),
                "--force",
                "--build-native",
            ],
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0, r.stdout + r.stderr
        seen = run_py(CODE)
        assert seen.returncode == 0, seen.stderr
        assert seen.stdout.split() == ["2", "2"]
    finally:
        # Restore the environment from the real checkout.
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "tools" / "dev" / "py_dev.py"),
                "--force",
                "--build-native",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
