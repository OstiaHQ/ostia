"""py-dev's up-to-date stamp covers everything an extension compiles against."""

import shutil

from tools.dev.py_dev import stamp

from .conftest import ROOT


def _copy(tmp_path):
    src = tmp_path / "src"
    for part in ("cmake", "telemetry", "fabric"):
        shutil.copytree(ROOT / part, src / part, ignore=shutil.ignore_patterns("__pycache__"))
    return src


def test_stamp_changes_with_the_components_public_headers(tmp_path):
    src = _copy(tmp_path)
    before = stamp(src, "fabric")
    header = src / "fabric" / "include" / "ostia" / "fabric" / "fabric.h"
    header.write_text(header.read_text() + "\n/* changed */\n")
    assert stamp(src, "fabric") != before


def test_stamp_changes_with_a_dependencys_public_headers(tmp_path):
    src = _copy(tmp_path)
    before = stamp(src, "fabric")
    header = src / "telemetry" / "include" / "ostia" / "telemetry" / "telemetry.h"
    header.write_text(header.read_text() + "\n/* changed */\n")
    assert stamp(src, "fabric") != before


def test_stamp_changes_with_the_build_modules(tmp_path):
    src = _copy(tmp_path)
    before = stamp(src, "telemetry")
    (src / "cmake" / "Dependencies.cmake").write_text("# changed\n")
    assert stamp(src, "telemetry") != before
