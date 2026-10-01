"""`ostia-dev remote k8s`: the default run and its subcommands (RFC-0005 §3.1, §4.1; P5)."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from ostia_dev.cli import app
from ostia_dev.errors import UsageError
from ostia_dev.remote import cli as remote_cli
from ostia_dev.remote.k8s import kubectl
from typer.testing import CliRunner

EXE = shutil.which("ostia-dev")


@pytest.fixture
def captured(monkeypatch, tmp_path):
    seen = {}

    def run_k8s(spec, cfg, repo):
        seen.update(spec=spec, repo=repo)
        return 0

    monkeypatch.setattr(remote_cli, "run_k8s", run_k8s)
    monkeypatch.setenv("OSTIA_CONFIG", str(tmp_path / "none.toml"))
    return seen


def _invoke(*args):
    return CliRunner().invoke(app, ["remote", "k8s", *args], env={"COLUMNS": "200"})


def _usage_error(r) -> str:
    assert isinstance(r.exception, UsageError) and r.exception.code == 2, r.output
    return r.exception.message


def test_flags_before_a_command_route_to_the_run(captured):
    r = _invoke("--context", "c1", "--profile", "cpu", "--", "ctest", "-L", "cpu")
    assert r.exit_code == 0, r.output
    spec = captured["spec"]
    assert (spec.backend, spec.profile, spec.command) == ("k8s", "cpu", ["ctest", "-L", "cpu"])
    assert spec.extra["context"] == "c1" and spec.envs == ["default"]


def test_gpu_profiles_default_to_cuda_12(captured):
    assert _invoke("--context", "c1", "--profile", "l4", "--suite", "gpu").exit_code == 0
    assert captured["spec"].envs == ["cuda-12"] and captured["spec"].suite == "gpu"


def test_k8s_flags_reach_the_spec(captured):
    r = _invoke(
        "--context",
        "arn:aws:eks:us-east-1:1:cluster/x",
        "--namespace",
        "n",
        "--profile",
        "cpu",
        "--schedule-timeout",
        "5m",
        "--cache",
        "--allow-unguarded",
        "--kubectl",
        "/opt/k",
        "--env",
        "default",
        "--env",
        "gcc11",
        "--yes",
    )
    assert r.exit_code == 0, r.output
    extra = captured["spec"].extra
    assert extra == {
        "context": "arn:aws:eks:us-east-1:1:cluster/x",
        "namespace": "n",
        "schedule_timeout": "5m",
        "cache": True,
        "allow_unguarded": True,
        "kubectl": "/opt/k",
        "keep": None,
        "pods": 1,
        "same_node": False,
    }
    assert captured["spec"].envs == ["default", "gcc11"] and captured["spec"].yes


@pytest.mark.parametrize(
    ("args", "keep"),
    [(["--keep-on-failure"], "30m"), (["--keep-on-failure=10m"], "10m"), ([], None)],
)
def test_keep_on_failure_takes_an_optional_value(captured, args, keep):
    assert _invoke("--context", "c1", "--profile", "cpu", *args).exit_code == 0
    assert captured["spec"].extra["keep"] == keep


def test_a_bare_keep_on_failure_after_the_double_dash_is_the_commands(captured):
    _invoke("--context", "c1", "--profile", "cpu", "--", "tool", "--keep-on-failure")
    assert captured["spec"].command == ["tool", "--keep-on-failure"]


def test_profile_is_required(captured):
    assert "--profile" in _usage_error(_invoke("--context", "c1"))


def test_preset_with_suite_is_exit_2(captured):
    assert "--preset" in _usage_error(
        _invoke("--context", "c1", "--profile", "cpu", "--suite", "cpu", "--preset", "dev")
    )


def test_group_help_lists_the_subcommands_and_the_run():
    r = _invoke("--help")
    out = re.sub(r"\x1b\[[0-9;]*m", "", r.output)
    assert r.exit_code == 0
    assert "kubectl" in out and "[run flags]" in out


def test_run_help_lists_every_k8s_flag():
    r = _invoke("run", "--help")
    out = re.sub(r"\x1b\[[0-9;]*m", "", r.output)
    for flag in (
        "--context",
        "--namespace",
        "--profile",
        "--schedule-timeout",
        "--cache",
        "--keep-on-failure",
        "--allow-unguarded",
        "--kubectl",
        "--env",
        "--suite",
        "--no-build",
        "--no-test",
        "--timeout",
        "--ref",
        "--env-var",
        "--allow-secret",
        "--results",
        "--yes",
        "--verbose",
    ):
        assert flag in out, flag


def test_kubectl_prints_the_path_and_version(monkeypatch):
    monkeypatch.setattr(kubectl, "ensure", lambda **kw: Path("/cache/kubectl"))
    r = _invoke("kubectl")
    assert r.exit_code == 0 and r.output.strip() == f"/cache/kubectl {kubectl.VERSION}"


def test_kubectl_update_shas(monkeypatch):
    monkeypatch.setattr(kubectl, "update_shas", lambda: {"linux-amd64": "a" * 64})
    r = _invoke("kubectl", "--update-shas")
    assert r.exit_code == 0 and "linux-amd64 " + "a" * 64 in r.output


def test_installed_missing_context_is_exit_2_before_any_kubectl_call(tmp_path):
    r = subprocess.run(
        [EXE, "remote", "k8s", "--profile", "cpu"],
        capture_output=True,
        text=True,
        env={
            "OSTIA_CONFIG": str(tmp_path / "none.toml"),
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
        },
        timeout=60,
    )
    assert r.returncode == 2 and "--context" in r.stderr and "Traceback" not in r.stderr
    assert not (tmp_path / ".cache" / "ostia" / "kubectl").exists()


@pytest.fixture
def admin_calls(monkeypatch, tmp_path):
    from ostia_dev.remote.k8s import admin
    from ostia_dev.remote.k8s.preflight import Target

    calls = []
    target = Target(context="c1", namespace="n1", provider="gke", kubectl=Path("/k"))
    monkeypatch.setattr(
        remote_cli,
        "_admin_target",
        lambda context, namespace, kubectl_path, cfg: (
            calls.append(("target", context, namespace)) or (target, "KUBE")
        ),
    )
    for name, ret in (
        ("init", ["applied Namespace/n1"]),
        ("verify", 0),
        ("cleanup", ["nothing to delete"]),
        ("profiles", "l4: gpu"),
        ("usage", "c1  l4  1 runs"),
    ):
        monkeypatch.setattr(
            admin, name, lambda *a, _n=name, _r=ret, **kw: calls.append((_n, a, kw)) or _r
        )
    monkeypatch.setenv("OSTIA_CONFIG", str(tmp_path / "none.toml"))
    return calls


def test_init_routes_with_privileged(admin_calls):
    r = _invoke("init", "--context", "c1", "--namespace", "n1", "--privileged")
    assert r.exit_code == 0, r.output
    assert "applied Namespace/n1" in r.output
    (name, args, kw) = admin_calls[-1]
    assert name == "init" and kw["privileged"] is True


def test_verify_exit_code_is_the_probes(admin_calls):
    assert _invoke("verify", "--context", "c1", "--namespace", "n1").exit_code == 0
    assert admin_calls[-1][2]["profile"] == "cpu"


def test_cleanup_flags(admin_calls):
    r = _invoke(
        "cleanup",
        "--context",
        "c1",
        "--namespace",
        "n1",
        "--run-id",
        "r1",
        "--cache",
        "--delete-namespace",
        "--yes",
    )
    assert r.exit_code == 0, r.output
    kw = admin_calls[-1][2]
    assert (kw["run_id"], kw["cache"], kw["delete_namespace"], kw["yes"], kw["all_"]) == (
        "r1",
        True,
        True,
        True,
        False,
    )


def test_profiles_without_a_context_needs_no_cluster(admin_calls):
    r = _invoke("profiles")
    assert r.exit_code == 0 and "l4: gpu" in r.output
    assert not [c for c in admin_calls if c[0] == "target"]


def test_profiles_with_a_context(admin_calls):
    assert _invoke("profiles", "--context", "c1").exit_code == 0
    assert admin_calls[0][0] == "target" and admin_calls[-1][2]["kube"] == "KUBE"


def test_usage(admin_calls, tmp_path):
    r = _invoke("usage", "--results", str(tmp_path))
    assert r.exit_code == 0 and "c1  l4  1 runs" in r.output


def test_pods_accepts_only_one_or_two(captured):
    r = _invoke("--context", "c1", "--profile", "cpu", "--pods", "3", "--", "true")
    assert "--pods" in _usage_error(r)


def test_same_node_needs_two_pods(captured):
    r = _invoke("--context", "c1", "--profile", "cpu", "--same-node", "--", "true")
    assert "--same-node" in _usage_error(r)


def test_pods_and_same_node_reach_the_spec(captured):
    r = _invoke("--context", "c1", "--profile", "cpu", "--pods", "2", "--same-node", "--", "true")
    assert r.exit_code == 0, r.output
    extra = captured["spec"].extra
    assert extra["pods"] == 2 and extra["same_node"] is True


def test_container_has_no_pods_option(captured):
    r = CliRunner().invoke(app, ["remote", "container", "--pods", "2", "--", "true"])
    assert r.exit_code == 2
