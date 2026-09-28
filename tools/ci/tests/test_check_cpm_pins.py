"""Tests for tools/ci/check_cpm_pins.py (RFC-0001 §2.4)."""

from pathlib import Path

import pytest

from tools.ci.check_cpm_pins import main, scan

SHA = "063de7e9578f82b369302001269680b4b1553359"


def write(root: Path, text: str, rel: str = "cmake/Dependencies.cmake") -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


GOOD = f"""
CPMAddPackage(
  NAME bar
  GITHUB_REPOSITORY foo/bar
  VERSION 1.2.3
  GIT_TAG {SHA} # v1.2.3
)
"""

BAD = {
    "tag_without_sha": "CPMAddPackage(\n  NAME bar\n  VERSION 1.2.3\n  GIT_TAG v1.2.3\n)\n",
    "sha_without_tag_comment": f"CPMAddPackage(\n  NAME bar\n  VERSION 1.2.3\n  GIT_TAG {SHA}\n)\n",
    "short_form": 'CPMAddPackage("gh:foo/bar@1.0")\n',
    "short_sha": "CPMAddPackage(\n  NAME bar\n  VERSION 1.2.3\n  GIT_TAG 063de7e # v1.2.3\n)\n",
    "version_mismatch": f"CPMAddPackage(\n  NAME bar\n  VERSION 1.2.4\n  GIT_TAG {SHA} # v1.2.3\n)\n",
}


def test_pinned_call_passes(tmp_path):
    write(tmp_path, GOOD)
    assert scan(tmp_path) == []


@pytest.mark.parametrize("case", sorted(BAD))
def test_unpinned_calls_fail(tmp_path, case):
    write(tmp_path, BAD[case])
    assert len(scan(tmp_path)) == 1, case


def test_vendored_cpm_and_build_dirs_are_skipped(tmp_path):
    write(tmp_path, BAD["short_form"], "cmake/CPM.cmake")
    write(tmp_path, BAD["short_form"], "build/default/_deps/x/CMakeLists.txt")
    assert scan(tmp_path) == []


def test_commented_calls_are_ignored(tmp_path):
    write(tmp_path, "#   CPMAddPackage(NAME foo ... OPTIONS \"X 1\")\n")
    assert scan(tmp_path) == []


def test_message_follows_contract(tmp_path, capsys):
    write(tmp_path, "\n" * 11 + BAD["tag_without_sha"])
    assert main(["--root", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "error: cmake/Dependencies.cmake:12 CPMAddPackage(NAME bar) is not pinned" in out
    assert "see: RFC-0001 §2.4" in out


def test_repository_is_pinned():
    assert scan(Path(__file__).resolve().parents[3]) == []
