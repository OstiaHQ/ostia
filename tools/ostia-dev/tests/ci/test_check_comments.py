"""Tests for ostia_dev/ci/check_comments.py (ADR-0015)."""

import io
import json
import subprocess
from pathlib import Path

import pytest
from ostia_dev.ci.check_comments import check_text, language, main

REPO = Path(__file__).resolve().parents[4]


def rules(text: str, lang: str, hook: bool = False) -> list[str]:
    return [f.rule for f in check_text(text, lang, "x", hook=hook)]


FLAGGED = [
    ("py", "# x = compute(y)\nz = 1\n", "commented-code"),
    ("py", "# import os\n", "commented-code"),
    ("py", "# for item in items:\n", "commented-code"),
    ("cpp", "// int fd = open(path);\nint a;\n", "commented-code"),
    ("cmake", "# set(FOO 1)\n", "commented-code"),
    ("py", "# --------------------\n", "banner"),
    ("cpp", "//////////////\n", "banner"),
    ("py", "# Now we increment i\ni += 1\n", "narration"),
    ("py", "# Step 2: parse the file\n", "narration"),
    ("cpp", "// First, we open the socket\n", "narration"),
    ("py", "# Added the retry as requested\n", "changelog"),
    ("cpp", "// Fixed the race in the queue\n", "changelog"),
    ("py", "timeout = 5  # (new)\n", "changelog"),
    ("py", "# TODO: handle errors\n", "todo"),
    ("py", "\n# Increment the counter\ncounter += 1\n", "restates"),
    ("cpp", "// Return the rank\nreturn rank_;\n", "restates"),
]


@pytest.mark.parametrize("lang,text,rule", FLAGGED)
def test_flagged(lang, text, rule):
    assert rules(text, lang) == [rule]


CLEAN = [
    ("py", "# The worker may exit first, so close() tolerates a closed pipe.\npipe.close()\n"),
    ("py", "# RFC-0001 §3.3: only the component's own files are checked.\nscan(root)\n"),
    ("cpp", "int timeout_ms = 50;  // milliseconds, per ucp_worker_progress spin\n"),
    ("cpp", "// Callers own the returned buffer and free it with ostia_fabric_free().\n"),
    ("cmake", "# ostia_fail(PROBLEM <s> [DETAILS <line>...] RULE <s> FIX <s> SEE <s>)\n"),
    ("py", "# TODO(#42): drop once CMake 3.30 is the minimum\n"),
    ("py", "x = '# not a comment'\n"),
    ("cpp", 'const char* s = "// not a comment";\n'),
    ("cpp", "/* int a = 1; */\n"),
    ("py", "#!/usr/bin/env python3\n"),
    ("py", "import x  # noqa: F401\n"),
    ("py", "# x = 1  comment-ok\n"),
    ("cpp", "// NOLINT(readability-x)\n"),
    ("py", "# fix: pixi run ostia-dev fmt\n"),
    ("py", "# Fixed at build time; see ADR-0013.\n"),
    # A multi-line explanation is not a label, even if a line echoes the code.
    ("py", "# Return the rank\n# because the caller indexes by it.\nreturn rank\n"),
]


@pytest.mark.parametrize("lang,text", CLEAN)
def test_clean(lang, text):
    assert rules(text, lang) == []


def test_named_banner_only_in_hook_mode():
    text = "// ---- Memory ----------------\n"
    assert rules(text, "cpp") == []
    assert rules(text, "cpp", hook=True) == ["banner"]


def test_density_only_in_hook_mode():
    text = "".join(f"# why {i} matters here\nx{i} = {i}\n" for i in range(6))
    assert "density" not in rules(text, "py")
    assert rules(text, "py", hook=True) == ["density"]


def test_language():
    assert language("a/b.cu") == "cpp"
    assert language("a/CMakeLists.txt") == "cmake"
    assert language("a.py") == "py"
    assert language("a.md") is None


def hook(payload, capsys) -> tuple[int, str]:
    code = main(["--hook"], stdin=io.StringIO(json.dumps(payload)))
    return code, capsys.readouterr().err


def test_hook_edit_reports_file_line(tmp_path, capsys):
    f = tmp_path / "a.py"
    f.write_text("a = 1\n# Now we increment i\ni += 1\n")
    payload = {
        "tool_name": "Edit",
        "tool_input": {"file_path": str(f), "new_string": "# Now we increment i\ni += 1\n"},
    }
    code, err = hook(payload, capsys)
    assert code == 2
    assert "narration" in err and "a.py:2:" in err
    assert "The edit was applied" in err


def test_hook_ignores_old_comments(tmp_path, capsys):
    f = tmp_path / "a.py"
    f.write_text("# Now we do the old thing\nb = 2\n")
    payload = {"tool_name": "Edit", "tool_input": {"file_path": str(f), "new_string": "b = 2\n"}}
    assert hook(payload, capsys) == (0, "")


def test_hook_write_and_multiedit(tmp_path, capsys):
    f = tmp_path / "a.cpp"
    write = {"tool_name": "Write", "tool_input": {"file_path": str(f), "content": "// ======\n"}}
    assert hook(write, capsys)[0] == 2
    edits = [{"new_string": "int a;\n"}, {"new_string": "// TODO fix\n"}]
    multi = {"tool_name": "MultiEdit", "tool_input": {"file_path": str(f), "edits": edits}}
    code, err = hook(multi, capsys)
    assert code == 2 and "todo" in err


def test_hook_skips_other_files_and_bad_input(capsys):
    md = {"tool_name": "Write", "tool_input": {"file_path": "README.md", "content": "# ----\n"}}
    assert hook(md, capsys)[0] == 0
    assert main(["--hook"], stdin=io.StringIO("not json")) == 0
    assert main(["--hook"], stdin=io.StringIO("[]")) == 0


def test_files_mode(tmp_path, capsys):
    f = tmp_path / "a.py"
    f.write_text("# Step 1: go\n")
    assert main([str(f)]) == 1
    out = capsys.readouterr().out
    assert "error: narration: comment breaks ADR-0015" in out
    assert "see: ADR-0015" in out


def test_tree_is_clean(capsys):
    listed = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-z"], capture_output=True, check=True
    ).stdout.decode()
    files = [
        REPO / f
        for f in listed.split("\0")
        if f and language(f) and "/tests/golden/" not in f and f != "cmake/CPM.cmake"
    ]
    files = [f for f in files if f.is_file()]
    assert main([str(f) for f in files]) == 0, capsys.readouterr().out
