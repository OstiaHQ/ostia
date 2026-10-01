"""Tests for ostia_dev/ci/lint.py (ADR-0013, RFC-0001 §4.1 lint row)."""

import shutil
import subprocess
from pathlib import Path

from ostia_dev.ci.lint import file_lists, main

REPO = Path(__file__).resolve().parents[4]


def git_repo(root: Path, files: dict[str, str]) -> None:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)


def test_file_lists(tmp_path):
    git_repo(
        tmp_path,
        {
            "a.cpp": "int a;\n",
            "k.cu": "int k;\n",
            "x/y/CMakeLists.txt": "project(x)\n",
            "cmake/CPM.cmake": "# vendored\n",
            "cmake/Other.cmake": "set(a 1)\n",
            "t.py": "a = 1\n",
            "gone.cpp": "int g;\n",
        },
    )
    (tmp_path / "gone.cpp").unlink()  # tracked but deleted: skipped
    lists = file_lists(tmp_path)
    assert lists["cpp"] == ["a.cpp", "k.cu"]
    assert lists["cmake"] == ["cmake/Other.cmake", "x/y/CMakeLists.txt"]
    assert lists["python"] == ["t.py"]


def test_bad_format_reports_the_fix(tmp_path, capsys):
    git_repo(tmp_path, {"a.cpp": "int   main( ){return 0;}\n"})
    shutil.copy(REPO / ".clang-format", tmp_path / ".clang-format")
    assert main(["lint", "--root", str(tmp_path), "--only", "clang-format"]) == 1
    out = capsys.readouterr().out
    assert "error: clang-format found problems" in out
    assert "fix: pixi run ostia-dev fmt" in out
    assert "lint: 1 checks, 1 failed" in out


def test_fmt_fixes_what_lint_reports(tmp_path, capsys):
    git_repo(tmp_path, {"a.cpp": "int   main( ){return 0;}\n"})
    shutil.copy(REPO / ".clang-format", tmp_path / ".clang-format")
    assert main(["fmt", "--root", str(tmp_path), "--only", "clang-format"]) == 0
    assert main(["lint", "--root", str(tmp_path), "--only", "clang-format"]) == 0


def test_comments_check(tmp_path, capsys):
    git_repo(tmp_path, {"a.py": "# Now we increment i\ni += 1\n", "cmake/CPM.cmake": "# ----\n"})
    assert main(["lint", "--root", str(tmp_path), "--only", "comments"]) == 1
    out = capsys.readouterr().out
    assert "error: narration: comment breaks ADR-0015" in out
    assert "CPM.cmake" not in out
    assert "lint: 1 checks, 1 failed" in out
