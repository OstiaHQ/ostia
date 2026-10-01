"""docs index and docs figures (RFC-0005 §2.2)."""

import os

from fakes.steps import RecordingRunner
from ostia_dev import paths
from ostia_dev.cli import app
from ostia_dev.dev import steps
from ostia_dev.errors import UsageError
from typer.testing import CliRunner


def test_docs_index_check_passes_on_the_repo():
    result = CliRunner().invoke(app, ["docs", "index", "--check"])
    assert result.exit_code == 0, result.output


def test_docs_index_from_a_subdirectory(monkeypatch):
    monkeypatch.chdir(paths.ROOT / "docs")
    assert CliRunner().invoke(app, ["docs", "index", "--check"]).exit_code == 0


def test_figures_without_bun_is_usage_error(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    result = CliRunner().invoke(app, ["docs", "figures"])
    assert isinstance(result.exception, UsageError)
    assert "bun" in result.exception.message and "fix:" in result.exception.message


def test_figures_defaults(monkeypatch, tmp_path):
    (tmp_path / "bun").write_text("#!/bin/sh\n")
    (tmp_path / "bun").chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    runner = RecordingRunner()
    monkeypatch.setattr(steps, "RUNNER", runner)
    assert CliRunner().invoke(app, ["docs", "figures"]).exit_code == 0
    [argv] = runner.calls
    assert argv[0] == "bun" and argv[1].endswith("ostia_dev/docs/render-figures.js")
    assert argv[2:] == [
        str(paths.ROOT / "docs/product/figures/src"),
        str(paths.ROOT / "docs/product/figures"),
    ]
