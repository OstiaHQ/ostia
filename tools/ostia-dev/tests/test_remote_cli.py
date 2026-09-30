"""`ostia-dev remote container`: flags and their errors (RFC-0005 §3.1, §5)."""

import pytest
from ostia_dev.cli import app
from ostia_dev.errors import UsageError
from ostia_dev.remote import cli as remote_cli
from typer.testing import CliRunner


@pytest.fixture
def captured(monkeypatch, tmp_path):
    """Replace drive() so the CLI tests see the RunSpec and backend without running."""
    seen = {}

    def drive(backend, spec, *, cfg, repo):
        seen.update(backend=backend, spec=spec, repo=repo)
        return 0

    monkeypatch.setattr(remote_cli, "drive", drive)
    monkeypatch.setenv("OSTIA_CONFIG", str(tmp_path / "none.toml"))
    return seen


def _invoke(*args):
    return CliRunner().invoke(app, ["remote", "container", *args])


def test_defaults(captured):
    r = _invoke()
    assert r.exit_code == 0, r.output
    spec = captured["spec"]
    assert (spec.backend, spec.profile, spec.envs) == ("container", "cpu", ["default"])
    assert spec.command is None and spec.suite is None and spec.preset is None
    assert spec.results.name == "remote" and spec.results.parent.name == "build"
    assert captured["backend"].requested is None and captured["backend"].gpus is False


def test_gpu_profile_defaults_to_cuda_12(captured):
    assert _invoke("--gpus", "--profile", "l4").exit_code == 0
    assert captured["spec"].envs == ["cuda-12"] and captured["backend"].gpus


def test_repeated_env_and_the_check_cuda_replacement(captured):
    r = _invoke("--env", "cuda-12", "--env", "cuda-13", "--preset", "release", "--no-test")
    assert r.exit_code == 0, r.output
    spec = captured["spec"]
    assert spec.envs == ["cuda-12", "cuda-13"] and spec.preset == "release" and spec.no_test


def test_command_after_double_dash(captured):
    assert _invoke("--no-build", "--", "ctest", "-L", "gpu", "--timeout", "5").exit_code == 0
    assert captured["spec"].command == ["ctest", "-L", "gpu", "--timeout", "5"]
    assert captured["spec"].no_build


def test_engine_and_env_vars(captured):
    r = _invoke("--engine", "docker", "--env-var", "A=1", "--env-var", "B=2", "--suite", "cpu")
    assert r.exit_code == 0, r.output
    assert captured["backend"].requested == "docker"
    assert captured["spec"].env_vars == ["A=1", "B=2"] and captured["spec"].suite == "cpu"


def test_unknown_engine_is_a_usage_error(captured):
    assert _invoke("--engine", "lxc").exit_code == 2


def _usage_error(r) -> str:
    """CliRunner skips OstiaApp.__call__, so the OstiaError is the result's exception."""
    assert isinstance(r.exception, UsageError) and r.exception.code == 2, r.output
    return r.exception.message


def test_preset_with_suite_is_exit_2(captured):
    assert "--preset" in _usage_error(_invoke("--suite", "cpu", "--preset", "release"))


def test_timeout_is_parsed(captured):
    assert _invoke("--timeout", "90m").exit_code == 0
    assert captured["spec"].timeout == "90m"
    assert "duration" in _usage_error(_invoke("--timeout", "soon"))


def test_verbose_turns_on_the_echo(captured):
    from ostia_dev import proc

    try:
        assert _invoke("-v").exit_code == 0
        assert proc.verbose()
    finally:
        proc.set_verbose(False)


def test_help_lists_the_flags():
    r = CliRunner().invoke(app, ["remote", "container", "--help"])
    for flag in ("--env", "--preset", "--suite", "--no-build", "--no-test", "--timeout", "--ref",
                 "--env-var", "--allow-secret", "--results", "--yes", "--gpus", "--engine",
                 "--profile"):  # fmt: skip
        assert flag in r.output, flag
