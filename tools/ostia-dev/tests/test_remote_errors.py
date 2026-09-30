"""Argument-row failures through the installed command: nothing is created (RFC-0005,
Failure handling; they exit 2 before any engine call)."""

import shutil
import subprocess

import pytest

EXE = shutil.which("ostia-dev")


def _run(tmp_path, *args):
    env = {"OSTIA_CONFIG": str(tmp_path / "none.toml"), "PATH": "/usr/bin:/bin"}
    return subprocess.run([EXE, "remote", "container", *args], capture_output=True, text=True,
                          env=env, timeout=60)  # fmt: skip


def test_failure_secret_looking_env_var(tmp_path):
    r = _run(tmp_path, "--env-var", "GH_TOKEN=x")
    assert r.returncode == 2 and "--allow-secret" in r.stderr and "Traceback" not in r.stderr


def test_failure_ref_pr_with_env_var(tmp_path):
    r = _run(tmp_path, "--ref", "pr/3", "--env-var", "A=1")
    assert r.returncode == 2 and "contributor code" in r.stderr


@pytest.mark.parametrize("arg", ["a\nb", "a\tb"])
def test_failure_newline_or_tab_in_the_command(tmp_path, arg):
    r = _run(tmp_path, "--", "python", "-c", arg)
    assert r.returncode == 2 and "script" in r.stderr
