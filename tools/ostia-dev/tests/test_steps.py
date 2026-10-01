"""Composite commands run a step list in order at the repository root, inside pixi
(RFC-0005 §2.2, rulings B4 and B5)."""

import os
import shutil
import subprocess

import pytest
from fakes.steps import RecordingRunner
from ostia_dev import paths
from ostia_dev.cli import app
from ostia_dev.dev import steps
from typer.testing import CliRunner

ENV = {"PIXI_ENVIRONMENT_NAME": "default", "OSTIA_BUILD_ROOT": "/b", "CONDA_PREFIX": "/p"}


@pytest.fixture
def pixi_env(monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)


def _invoke(monkeypatch, argv, runner):
    monkeypatch.setattr(steps, "RUNNER", runner)
    return CliRunner().invoke(app, argv)


def test_build_outside_pixi_is_usage_error():
    env = {k: v for k, v in os.environ.items() if k != "PIXI_ENVIRONMENT_NAME"}
    r = subprocess.run(
        [shutil.which("ostia-dev"), "build"], env=env, capture_output=True, text=True
    )
    assert r.returncode == 2
    assert "fix: pixi run ostia-dev build" in r.stderr


def test_configure_uses_the_callers_environment(pixi_env, monkeypatch):
    monkeypatch.setenv("PIXI_ENVIRONMENT_NAME", "clang")
    runner = RecordingRunner()
    assert _invoke(monkeypatch, ["build"], runner).exit_code == 0
    assert runner.calls[0] == ["pixi", "run", "--frozen", "-e", "clang", "_configure"]


def test_steps_run_at_repo_root_from_a_subdir(pixi_env, monkeypatch):
    monkeypatch.chdir(paths.ROOT / "docs")
    runner = RecordingRunner()
    assert _invoke(monkeypatch, ["test"], runner).exit_code == 0
    assert runner.cwds and all(cwd == paths.ROOT for cwd in runner.cwds)


def test_sigint_stops_the_step_list_with_130(pixi_env, monkeypatch):
    runner = RecordingRunner(interrupt_at=1)
    result = _invoke(monkeypatch, ["build"], runner)
    assert result.exit_code == 130
    assert len(runner.calls) == 2
    assert "Traceback" not in result.output


def test_failure_code_passes_through(pixi_env, monkeypatch):
    runner = RecordingRunner(fail_at=1, code=8)
    assert _invoke(monkeypatch, ["test", "cpp"], runner).exit_code == 8
    assert len(runner.calls) == 2
