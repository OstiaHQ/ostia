"""The k8s preflight: context, namespace, provider, guardrails, fit (RFC-0005 §4.1, §4.3, §4.4)."""

import io
import sys
import tomllib
from pathlib import Path

import pytest
from fakes.clock import FakeClock
from fakes.kube import FakeKube, forbid
from ostia_dev import config
from ostia_dev.errors import UsageError
from ostia_dev.remote import core, profiles
from ostia_dev.remote.k8s import manifests, preflight

FIXTURES = Path(__file__).parent / "fixtures" / "kube"
EKS = "arn:aws:eks:us-east-1:123456789012:cluster/ostia"


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.fixture
def answers(monkeypatch):
    def give(text: str) -> None:
        monkeypatch.setattr(sys, "stdin", _Tty(text))

    return give


@pytest.fixture(autouse=True)
def no_tty(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))


@pytest.fixture
def cfg_path(tmp_path):
    return tmp_path / "config.toml"


def _cfg(cfg_path, text=""):
    if text:
        cfg_path.write_text("schema = 1\n" + text)
    return config.load(cfg_path)


@pytest.fixture
def fake(tmp_path):
    k = FakeKube(FakeClock(), root=tmp_path / "pods")
    k.load(FIXTURES / "nodes-gke-l4.json")
    return k


def _guard(fake, **kw):
    for obj in manifests.guardrails("ostia-test", **kw):
        fake.apply(obj, record=False)


def _spec(**extra):
    base = {"context": "gke_p_z_c"}
    base.update(extra)
    return core.RunSpec(
        backend="k8s", profile="l4", envs=["cuda-12"], results=Path("r"), extra=base
    )


def _resolve(fake, cfg, spec=None):
    def factory(kubectl, context, namespace, provider=None):
        fake.context, fake.namespace = context, namespace
        return fake

    return preflight.resolve(
        spec or _spec(), cfg, kube_factory=factory, ensure_kubectl=lambda **kw: Path("/opt/kubectl")
    )


GKE = '[remote.k8s.contexts.gke_p_z_c]\nprovider = "gke"\nnamespace = "ostia-test"\n'


def test_failure_no_context(fake, cfg_path):
    with pytest.raises(UsageError) as e:
        _resolve(fake, _cfg(cfg_path), _spec(context=None))
    assert "--context" in e.value.message and e.value.code == 2


def test_failure_no_namespace_no_tty(fake, cfg_path):
    with pytest.raises(UsageError) as e:
        _resolve(fake, _cfg(cfg_path, '[remote.k8s.contexts.gke_p_z_c]\nprovider = "gke"\n'))
    assert "--namespace" in e.value.message
    assert "[remote.k8s.contexts" in e.value.message


def test_failure_unknown_context_no_tty(fake, cfg_path):
    with pytest.raises(UsageError) as e:
        _resolve(fake, _cfg(cfg_path), _spec(namespace="ostia-test"))
    assert 'provider = "gke"' in e.value.message
    assert '[remote.k8s.contexts."gke_p_z_c"]' in e.value.message
    assert not cfg_path.exists()  # never written without asking


def test_tty_prompts_write_the_config(fake, cfg_path, answers):
    answers("\n\n\n")  # namespace [ostia-test], save [Y], save provider [Y]
    target, _ = _resolve(fake, _cfg(cfg_path))
    assert (target.namespace, target.provider) == ("ostia-test", "gke")
    saved = tomllib.loads(cfg_path.read_text())["remote"]["k8s"]["contexts"]["gke_p_z_c"]
    assert saved == {"namespace": "ostia-test", "provider": "gke"}


def test_saved_context_is_found_on_the_next_run(fake, cfg_path, answers):
    answers("\n\n\n")
    _resolve(fake, _cfg(cfg_path), _spec(context=EKS))
    sys.stdin = io.StringIO("")  # no TTY on the second run: nothing may be asked
    target, _ = _resolve(fake, _cfg(cfg_path), _spec(context=EKS))
    assert (target.context, target.namespace, target.provider) == (EKS, "ostia-test", "gke")


@pytest.mark.parametrize(
    ("fixture", "provider"),
    [
        ("nodes-gke-l4.json", "gke"),
        ("nodes-eks-g6.json", "eks"),
        ("nodes-eks-karpenter.json", "eks"),
        ("nodes-aks.json", "aks"),
        ("nodes-generic.json", None),
    ],
)
def test_detect_provider(fixture, provider):
    import json

    nodes = json.loads((FIXTURES / fixture).read_text())["items"]
    assert preflight.detect_provider(nodes) == provider


def test_no_node_access_asks_for_the_provider(fake, cfg_path, answers):
    fake.script([(0, forbid("list", "node"))])
    answers("eks\nn\n")  # provider, don't save
    target, _ = _resolve(
        fake, _cfg(cfg_path, '[remote.k8s.contexts.gke_p_z_c]\nnamespace = "ostia-test"\n')
    )
    assert target.provider == "eks" and target.node_access is False


def test_no_node_access_and_no_tty_is_exit_2(fake, cfg_path):
    fake.script([(0, forbid("list", "node"))])
    with pytest.raises(UsageError) as e:
        _resolve(
            fake, _cfg(cfg_path, '[remote.k8s.contexts.gke_p_z_c]\nnamespace = "ostia-test"\n')
        )
    assert "provider" in e.value.message


def _check(fake, cfg_path, profile="l4", yes=False, allow_unguarded=False):
    cfg = _cfg(cfg_path, GKE)
    target, kube = _resolve(fake, cfg, _spec(allow_unguarded=allow_unguarded))
    return target, preflight.check(target, kube, profiles.resolve(profile, "gke", cfg), yes=yes)


def test_all_good(fake, cfg_path):
    _guard(fake)
    target, notes = _check(fake, cfg_path)
    assert notes == [] and target.allow_unguarded is False


def test_failure_namespace_missing_no_tty_no_yes(fake, cfg_path):
    with pytest.raises(UsageError) as e:
        _check(fake, cfg_path)
    assert "--yes" in e.value.message and "init" in e.value.message


def test_yes_creates_a_missing_namespace_with_guardrails(fake, cfg_path):
    _, notes = _check(fake, cfg_path, yes=True)
    ns = fake.get("namespace", "ostia-test", namespaced=False)
    assert ns["metadata"]["labels"]["ostia.dev/managed"] == "true"
    assert fake.get("serviceaccount", "ostia-test-runner")
    assert fake.list("resourcequota") and len(fake.list("networkpolicy")) == 2


@pytest.mark.parametrize("missing", ["psa", "quota", "policy"])
def test_failure_guardrails_missing(fake, cfg_path, missing):
    _guard(fake)
    if missing == "psa":
        ns = fake.get("namespace", "ostia-test", namespaced=False)
        del ns["metadata"]["labels"]["pod-security.kubernetes.io/enforce"]
        fake.apply(ns, record=False)
    elif missing == "quota":
        fake.delete("resourcequota", "ostia-test-quota")
    else:
        fake.delete("networkpolicy", selector="ostia.dev/managed=true")
    with pytest.raises(UsageError) as e:
        _check(fake, cfg_path)
    word = {"psa": "Pod Security", "quota": "ResourceQuota", "policy": "NetworkPolicy"}[missing]
    assert word in e.value.message and "--allow-unguarded" in e.value.message


def test_allow_unguarded_passes_and_is_recorded(fake, cfg_path):
    _guard(fake)
    fake.delete("resourcequota", "ostia-test-quota")
    target, notes = _check(fake, cfg_path, allow_unguarded=True)
    assert target.allow_unguarded is True
    assert any("ResourceQuota" in n for n in notes)


def test_failure_service_account_missing(fake, cfg_path):
    _guard(fake)
    fake.delete("serviceaccount", "ostia-test-runner")
    with pytest.raises(UsageError) as e:
        _check(fake, cfg_path)
    assert "ostia-test-runner" in e.value.message and "init" in e.value.message


def test_failure_rdma_profile_in_restricted_namespace(fake, cfg_path):
    _guard(fake)
    cfg = _cfg(
        cfg_path,
        GKE + '[remote.k8s.profiles.ib]\nkind = "rdma"\ncpu = "4"\n'
        'memory = "8Gi"\nephemeral_storage = "10Gi"\n[remote.k8s.profiles.ib.gke]\n'
        'node_selector = { "x" = "y" }\n',
    )
    target, kube = _resolve(fake, cfg)
    with pytest.raises(UsageError) as e:
        preflight.check(target, kube, profiles.resolve("ib", "gke", cfg), yes=False)
    assert "init --privileged" in e.value.message


def test_failure_profile_fits_no_node(fake, cfg_path):
    _guard(fake)
    cfg = _cfg(cfg_path, GKE + '[remote.k8s.profiles.l4]\nmemory = "40Gi"\n')
    target, kube = _resolve(fake, cfg)
    with pytest.raises(UsageError) as e:
        preflight.check(target, kube, profiles.resolve("l4", "gke", cfg), yes=False)
    msg = e.value.message
    assert "29Gi" in msg and "memory" in msg and "profiles.l4" in msg


def test_fit_check_skips_when_no_node_matches_yet(fake, cfg_path):
    _guard(fake)
    _, notes = _check(fake, cfg_path, profile="a100")  # an a100 pool scaled to zero
    assert any("no node matches" in n for n in notes)


@pytest.mark.parametrize(
    ("fixture", "warns"), [("version-skew-ok.json", False), ("version-skew-warn.json", True)]
)
def test_failure_kubectl_skew(fake, cfg_path, capsys, fixture, warns):
    _guard(fake)
    fake.version_fixture = fixture
    _, notes = _check(fake, cfg_path)
    warned = any("v1.36.5" in n and "v1.34.2" in n for n in notes)
    assert warned is warns
    if warns:
        assert "--kubectl" in capsys.readouterr().err


def test_yes_creates_a_namespace_with_the_detected_guardrails(fake, cfg_path):
    fake.apply(
        {
            "apiVersion": "apps/v1",
            "kind": "DaemonSet",
            "metadata": {"name": "node-local-dns", "labels": {"k8s-app": "node-local-dns"}},
        },
        record=False,
    )
    fake.apply(
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "ServiceCIDR",
            "metadata": {"name": "kubernetes"},
            "spec": {"cidrs": ["34.118.224.0/20"]},
        },
        record=False,
    )
    cfg = _cfg(cfg_path, GKE + '[remote.k8s.contexts.gke_p_z_c.quota]\n"pods" = "3"\n')
    target, kube = _resolve(fake, cfg)
    preflight.check(target, kube, profiles.resolve("l4", "gke", cfg), yes=True, cfg=cfg)
    (egress,) = [p for p in fake.list("networkpolicy") if p["metadata"]["name"] == "ostia-egress"]
    assert {"ipBlock": {"cidr": "169.254.20.10/32"}} in egress["spec"]["egress"][0]["to"]
    assert "34.118.224.0/20" in egress["spec"]["egress"][1]["to"][0]["ipBlock"]["except"]
    assert fake.get("resourcequota", "ostia-test-quota")["spec"]["hard"]["pods"] == "3"
