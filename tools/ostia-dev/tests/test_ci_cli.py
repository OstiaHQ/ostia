"""The ci commands: lint, fmt and check <verb> forward to the moved modules (RFC-0005 §2.2,
ruling B1)."""

import subprocess

from ostia_dev.cli import app
from typer.testing import CliRunner

VERBS = ["graph", "macros", "tidy", "layering", "cpm-pins", "exports", "comments", "docs-as-test"]


def _git_tree(root, files):
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)


def test_check_help_lists_every_verb():
    result = CliRunner().invoke(app, ["check", "--help"])
    assert result.exit_code == 0
    for verb in VERBS:
        assert verb in result.output, verb


def test_lint_only_passes_through(tmp_path):
    _git_tree(tmp_path, {"query/src/plan.cpp": "#include <ostia/fabric/topology.hpp>\n"})
    (tmp_path / "cmake").mkdir()
    (tmp_path / "cmake" / "layering.json").write_text(
        (__import__("ostia_dev.paths").paths.ROOT / "cmake" / "layering.json").read_text()
    )
    result = CliRunner().invoke(app, ["lint", "--only", "layering", "--root", str(tmp_path)])
    assert result.exit_code == 1
    assert "error: query/src/plan.cpp:1 includes <ostia/fabric/topology.hpp>" in result.output


def test_check_layering_help_is_argparse():
    result = CliRunner().invoke(app, ["check", "layering", "--help"])
    assert result.exit_code == 0
    assert result.output.startswith("usage: ostia-dev check layering")
