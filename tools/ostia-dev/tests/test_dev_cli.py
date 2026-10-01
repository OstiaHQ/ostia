"""build, test, py-dev, clean, doctor and hooks (RFC-0005 §2.2)."""

import sys

import pytest
from fakes.steps import RecordingRunner
from ostia_dev.cli import app
from ostia_dev.dev import steps
from ostia_dev.errors import UsageError
from typer.testing import CliRunner

CONFIGURE = ["pixi", "run", "--frozen", "-e", "default", "_configure"]
BUILD = ["cmake", "--build", "--preset", "dev"]


@pytest.fixture
def run(monkeypatch):
    for k, v in {
        "PIXI_ENVIRONMENT_NAME": "default",
        "OSTIA_BUILD_ROOT": "/b",
        "CONDA_PREFIX": "/p",
    }.items():
        monkeypatch.setenv(k, v)

    def invoke(*argv):
        runner = RecordingRunner()
        monkeypatch.setattr(steps, "RUNNER", runner)
        return CliRunner().invoke(app, list(argv)), runner.calls

    return invoke


def test_ctest_and_pytest_args_pass_through(run):
    result, calls = run("test", "cpp", "-R", "Result", "--output-on-failure")
    assert result.exit_code == 0
    assert calls[-1] == ["ctest", "--preset", "dev", "-R", "Result", "--output-on-failure"]
    result, calls = run("test", "py", "-k", "telemetry", "-x")
    assert calls[-1] == ["pytest", "-k", "telemetry", "-x"]


def test_test_selectors_are_exclusive(run):
    result, calls = run("test", "--levels", "--preset", "dev")
    assert isinstance(result.exception, UsageError) and result.exception.code == 2
    assert calls == []


def test_unknown_sanitizer_is_usage_error(run):
    result, calls = run("test", "--sanitize", "msan")
    assert isinstance(result.exception, UsageError) and "asan-ubsan" in result.exception.message
    assert calls == []


def test_label_runs_ctest_only(run):
    _, calls = run("test", "-L", "multiprocess")
    assert calls == [CONFIGURE, BUILD, ["ctest", "--preset", "dev", "-L", "multiprocess"]]


def test_py_dev_runs_the_old_install_native_sequence(run):
    _, calls = run("py-dev")
    assert calls == [
        CONFIGURE,
        BUILD,
        ["cmake", "--install", "/b/dev", "--prefix", "/p"],
        ["cmake", "-E", "copy", "/b/dev/install_manifest.txt", "/b/native-install-manifest.txt"],
        [sys.executable, "-m", "ostia_dev.dev.py_dev", "--preset", "dev"],
    ]


def test_py_dev_build_native_builds_itself(run):
    _, calls = run("py-dev", "--preset", "level-off", "--build-native", "--force")
    assert calls == [
        [
            sys.executable,
            "-m",
            "ostia_dev.dev.py_dev",
            "--preset",
            "dev",
            "--preset",
            "level-off",
            "--build-native",
            "--force",
        ]
    ]


def test_check_tidy_with_files(run):
    _, calls = run("check", "tidy", "a.cpp", "b.cpp")
    assert calls == [CONFIGURE, ["run-clang-tidy", "-quiet", "-p", "/b/dev", "a.cpp", "b.cpp"]]


def test_hooks_runs_pre_commit_install(run):
    _, calls = run("hooks")
    assert calls == [["pre-commit", "install"]]
