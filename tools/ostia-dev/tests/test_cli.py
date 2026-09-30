"""Tests for the ostia-dev entry point (RFC-0005 §1.1)."""

from importlib.metadata import version

from typer.testing import CliRunner

from ostia_dev.cli import app


def test_help_lists_remote():
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "remote" in result.output


def test_version_prints_the_package_version():
    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == f"ostia-dev {version('ostia-dev')}"
