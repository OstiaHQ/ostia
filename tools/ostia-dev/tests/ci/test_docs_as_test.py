"""Tests for ostia_dev/ci/docs_as_test.py (RFC-0001 §4.1 docs-as-test)."""

from pathlib import Path

from ostia_dev.ci.docs_as_test import blocks, main

DOC = """# Guide

```bash
echo not marked
```

<!-- docs-as-test:start -->
```bash
echo one
echo two
```
<!-- docs-as-test:end -->

Text.

<!-- docs-as-test:start -->
```sh
echo three
```
<!-- docs-as-test:end -->
"""


def test_extracts_only_marked_blocks():
    assert blocks(DOC) == ["echo one\necho two\n", "echo three\n"]


def test_unterminated_marker_is_an_error(tmp_path, capsys):
    doc = tmp_path / "g.md"
    doc.write_text("<!-- docs-as-test:start -->\n```bash\necho x\n```\n")
    assert main(["--doc", str(doc), "--cwd", str(tmp_path)]) == 1
    assert "no matching docs-as-test:end" in capsys.readouterr().out


def test_runs_blocks_and_records_duration(tmp_path, monkeypatch):
    doc = tmp_path / "g.md"
    doc.write_text(DOC.replace("echo one", "echo one > out.txt"))
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert main(["--doc", str(doc), "--cwd", str(tmp_path)]) == 0
    assert (tmp_path / "out.txt").read_text() == "one\n"
    assert "docs-as-test" in summary.read_text() and " s" in summary.read_text()


def test_a_failing_command_fails_the_run(tmp_path, capsys):
    doc = tmp_path / "g.md"
    doc.write_text(
        "<!-- docs-as-test:start -->\n```bash\nfalse\necho after\n```\n<!-- docs-as-test:end -->\n"
    )
    assert main(["--doc", str(doc), "--cwd", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "error: block 1 of" in out
    assert "after" not in out.splitlines()  # bash -e stopped at `false`


def test_building_guide_has_the_quick_start():
    guide = Path(__file__).resolve().parents[4] / "docs" / "guides" / "building.md"
    assert blocks(guide.read_text()) == [
        "pixi install\npixi run ostia-dev build\npixi run ostia-dev test\n"
    ]
