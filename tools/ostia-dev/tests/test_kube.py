"""The thin kubectl wrapper and its error mapping (RFC-0005 §4.1)."""

import io
import json
import subprocess
from pathlib import Path

import pytest
from fakes.clock import FakeClock
from fakes.kube import FakeKube, forbid, lose_connection
from ostia_dev.errors import InfraError, UsageError
from ostia_dev.remote.k8s import kube as kube_mod
from ostia_dev.remote.k8s.kube import Kube, KubeError, LostConnection

FIXTURES = Path(__file__).parent / "fixtures" / "kube"
KUBECTL = Path("/opt/kubectl")


class Runner:
    """Records argv; answers from a queue of (returncode, stdout, stderr)."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list[dict] = []

    def _next(self):
        return self.answers.pop(0) if self.answers else (0, "", "")

    def run(self, cmd, **kw):
        self.calls.append({"cmd": cmd, **kw})
        code, out, err = self._next()
        if "stdout" in kw and kw["stdout"] is not None:
            kw["stdout"].write(out if isinstance(out, bytes) else out.encode())
            out = None
        return subprocess.CompletedProcess(cmd, code, out, err)

    def stream(self, cmd, **kw):
        self.calls.append({"cmd": cmd, **kw})
        code, out, err = self._next()

        class P:
            stdout = iter(out.splitlines(keepends=True))
            stderr = io.StringIO(err)
            returncode = code

            def wait(self):
                return code

            def terminate(self):
                pass

        return P()


def _kube(runner, provider="gke"):
    return Kube(KUBECTL, "gke_p_z_c", "ostia-test", provider=provider, runner=runner)


def _argv(runner, i=-1):
    return runner.calls[i]["cmd"]


def test_version_is_cluster_wide():
    r = Runner((0, (FIXTURES / "version-skew-ok.json").read_text(), ""))
    assert _kube(r).version()["serverVersion"]["minor"] == "37"
    assert _argv(r) == [str(KUBECTL), "--context", "gke_p_z_c", "version", "-o", "json"]


CALLS = [
    ("get", lambda k: k.get("job", "j"), ["get", "job", "j", "-o", "json", "--ignore-not-found"]),
    ("list", lambda k: k.list("pod", selector="a=b"), ["get", "pod", "-l", "a=b", "-o", "json"]),
    ("apply", lambda k: k.apply({"kind": "Job"}), ["apply", "-f", "-", "-o", "json"]),
    ("create", lambda k: k.create({"kind": "Job"}), ["create", "-f", "-", "-o", "json"]),
    (
        "patch",
        lambda k: k.patch("job", "j", {"spec": {"suspend": False}}),
        [
            "patch",
            "job",
            "j",
            "--type",
            "merge",
            "-p",
            '{"spec": {"suspend": false}}',
            "-o",
            "json",
        ],
    ),
    (
        "delete",
        lambda k: k.delete("job", "j"),
        ["delete", "job", "j", "--ignore-not-found", "--wait=false", "--cascade=background"],
    ),
    (
        "delete-selector",
        lambda k: k.delete("job", selector="a=b", wait=True),
        ["delete", "job", "-l", "a=b", "--ignore-not-found", "--wait=true", "--cascade=background"],
    ),
    ("events", lambda k: k.events(), ["get", "events", "-o", "json"]),
    (
        "run_status",
        lambda k: k.run_status("r1"),
        ["get", "job,pods", "-l", "ostia.dev/run-id=r1", "-o", "json"],
    ),
    (
        "exec_in",
        lambda k: k.exec_in("p", ["tar", "-x"], io.BytesIO(b"t")),
        ["exec", "-i", "p", "--", "tar", "-x"],
    ),
    (
        "exec_out",
        lambda k: k.exec_out("p", ["sh", "-c", "x"]),
        ["exec", "p", "--", "sh", "-c", "x"],
    ),
    ("can_i", lambda k: k.can_i("create", "jobs"), ["auth", "can-i", "create", "jobs"]),
]


@pytest.mark.parametrize(("name", "call", "tail"), CALLS, ids=[c[0] for c in CALLS])
def test_every_call_passes_context_and_namespace(name, call, tail):
    out = '{"kind": "List", "items": []}' if name in ("list", "events", "run_status") else "{}"
    r = Runner((0, out, ""))
    call(_kube(r))
    assert _argv(r) == [str(KUBECTL), "--context", "gke_p_z_c", "--namespace", "ostia-test", *tail]


def test_cluster_scoped_calls_have_no_namespace():
    r = Runner((0, '{"kind": "List", "items": []}', ""), (0, "{}", ""))
    k = _kube(r)
    k.list("node", namespaced=False)
    k.get("namespace", "ostia-test", namespaced=False)
    assert all("--namespace" not in c["cmd"] for c in r.calls)


def test_apply_sends_the_manifest_on_stdin_never_in_argv():
    r = Runner((0, '{"kind": "Secret"}', ""))
    _kube(r).apply({"kind": "Secret", "stringData": {"TOKEN": "s3cret"}})
    assert "s3cret" not in " ".join(_argv(r))
    assert json.loads(r.calls[-1]["input"])["stringData"] == {"TOKEN": "s3cret"}


def test_not_found_get_is_none():
    assert _kube(Runner((0, "", ""))).get("job", "gone") is None


def test_can_i_no_is_false():
    assert _kube(Runner((1, "no\n", ""))).can_i("list", "nodes") is False


def test_forbidden_names_verb_resource_and_role():
    r = Runner((1, "", (FIXTURES / "stderr-forbidden.txt").read_text()))
    with pytest.raises(KubeError) as e:
        _kube(r).apply({"kind": "Job"})
    msg = e.value.message
    assert e.value.code == 2
    assert "create jobs" in msg and "ostia-test-developer" in msg and "gke_p_z_c" in msg
    assert "ostia-dev remote k8s init" in msg


@pytest.mark.parametrize(
    ("provider", "fix"),
    [
        ("gke", "gcloud components install gke-gcloud-auth-plugin"),
    ],
)
def test_missing_credential_plugin(provider, fix):
    r = Runner((1, "", (FIXTURES / "stderr-gke-plugin-missing.txt").read_text()))
    with pytest.raises(KubeError) as e:
        _kube(r, provider).version()
    assert "gke-gcloud-auth-plugin" in e.value.message and fix in e.value.message


@pytest.mark.parametrize(
    ("plugin", "fix"),
    [
        ("aws", "aws eks update-kubeconfig"),
        ("kubelogin", "az aks install-cli"),
    ],
)
def test_other_missing_plugins(plugin, fix):
    stderr = f"getting credentials: exec: executable {plugin} not found\n"
    with pytest.raises(KubeError) as e:
        _kube(Runner((1, "", stderr)), None).version()
    assert fix in e.value.message


@pytest.mark.parametrize(
    ("provider", "fix"),
    [
        ("gke", "gcloud auth login"),
        ("eks", "aws sso login"),
        ("aks", "az login"),
        (None, "log in to the cluster again"),
    ],
)
def test_expired_token(provider, fix):
    r = Runner((1, "", (FIXTURES / "stderr-token-expired.txt").read_text()))
    with pytest.raises(KubeError) as e:
        _kube(r, provider).list("pod")
    assert fix in e.value.message and e.value.code == 2


def test_connection_refused_is_lost_connection():
    r = Runner((1, "", (FIXTURES / "stderr-connection-refused.txt").read_text()))
    with pytest.raises(LostConnection) as e:
        _kube(r).run_status("r1")
    assert e.value.code == 3


def test_an_unknown_kubectl_failure_is_infra_with_its_stderr():
    r = Runner((1, "", "error: unable to upgrade connection: container not found\n"))
    with pytest.raises(InfraError) as e:
        _kube(r).exec_in("p", ["tar", "-x"], io.BytesIO(b""))
    assert "unable to upgrade connection" in e.value.message


def test_exec_failures_return_the_code():
    r = Runner((2, "", "tar: short read\n"))
    res = _kube(r).exec_in("p", ["tar", "-x"], io.BytesIO(b""), check=False)
    assert res.returncode == 2


def test_exec_out_gives_bytes():
    r = Runner((0, b"\x00tar", ""))
    assert _kube(r).exec_out("p", ["sh", "-c", "tar -cf - x"]).stdout == b"\x00tar"
    assert r.calls[-1]["binary"] is True


def test_logs_follow_yields_lines_and_resumes_since():
    r = Runner((0, "2026-10-02T14:20:00.000000001Z a\n2026-10-02T14:20:01.0Z b\n", ""))
    lines = list(_kube(r).logs_follow("p", since_time="2026-10-02T14:19:00Z"))
    assert lines == ["2026-10-02T14:20:00.000000001Z a", "2026-10-02T14:20:01.0Z b"]
    assert _argv(r)[-4:] == ["-f", "--timestamps", "--since-time=2026-10-02T14:19:00Z", "p"]
    assert r.calls[-1].get("merge_stderr") is False


def test_a_log_stream_that_ends_on_an_error_raises_lost_connection():
    r = Runner((1, "2026-10-02T14:20:00Z a\n", "error: unexpected EOF\n"))
    with pytest.raises(LostConnection):
        list(_kube(r).logs_follow("p"))


# The fake raises the same errors as the real wrapper (moved from Task 1, see the ledger).


def test_fake_forbid_raises_the_mapped_error(tmp_path):
    k = FakeKube(FakeClock(), root=tmp_path)
    k.script([(0, forbid("create", "jobs"))])
    with pytest.raises(KubeError) as e:
        k.apply({"kind": "Job", "metadata": {"name": "j"}, "spec": {}})
    assert "create jobs" in e.value.message and "ostia-test-developer" in e.value.message


def test_fake_lose_connection_then_recover(tmp_path):
    clock = FakeClock()
    k = FakeKube(clock, root=tmp_path)
    k.script([(0, lose_connection(for_seconds=10))])
    with pytest.raises(LostConnection):
        k.run_status("r1")
    clock.sleep(10)
    assert k.run_status("r1")["items"] == []


def test_errors_are_the_contract_types():
    assert issubclass(KubeError, UsageError) and issubclass(LostConnection, InfraError)
    assert kube_mod.forbidden_error("get", "pods", "ns", "ctx").code == 2
