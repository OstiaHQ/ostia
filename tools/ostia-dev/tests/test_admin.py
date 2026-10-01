"""`remote k8s init | verify | cleanup | profiles | usage` (RFC-0005 §4.1, §4.3, §4.6, §4.8)."""

import io
import json
import sys
from pathlib import Path

import pytest
from fakes.clock import FakeClock
from fakes.kube import FakeKube, log, pod_phase
from ostia_dev import config
from ostia_dev.errors import UsageError
from ostia_dev.remote import core
from ostia_dev.remote.k8s import admin, manifests
from ostia_dev.remote.k8s.preflight import Target

FIXTURES = Path(__file__).parent / "fixtures" / "kube"


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def fake(clock, tmp_path):
    k = FakeKube(clock, root=tmp_path / "pods")
    k.load(FIXTURES / "nodes-gke-l4.json")
    return k


@pytest.fixture
def target():
    return Target(context="c1", namespace="ostia-test", provider="gke", kubectl=Path("/k"))


@pytest.fixture
def cfg(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text(
        'schema = 1\n[remote.k8s.contexts.c1]\nblocked_cidrs = ["203.0.113.0/24"]\n'
        '[remote.k8s.contexts.c1.quota]\n"requests.nvidia.com/gpu" = "8"\n'
    )
    return config.load(path)


@pytest.fixture(autouse=True)
def no_tty(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))


def _kinds(fake):
    return sorted({k[0] for k in fake.store})


def test_init_creates_every_guardrail_and_prints_the_bindings(fake, target, cfg):
    fake.apply(
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "ServiceCIDR",
            "metadata": {"name": "kubernetes"},
            "spec": {"cidrs": ["34.118.224.0/20"]},
        },
        record=False,
    )
    lines = admin.init(target, fake, cfg=cfg)
    assert {
        "namespace",
        "serviceaccount",
        "resourcequota",
        "limitrange",
        "networkpolicy",
        "role",
        "clusterrole",
    } <= set(_kinds(fake))
    (egress,) = [p for p in fake.list("networkpolicy") if p["metadata"]["name"] == "ostia-egress"]
    blocked = egress["spec"]["egress"][1]["to"][0]["ipBlock"]["except"]
    assert {"34.118.224.0/20", "10.52.3.0/24", "203.0.113.0/24"} <= set(blocked)
    quota = fake.get("resourcequota", "ostia-test-quota")["spec"]["hard"]
    assert quota["requests.nvidia.com/gpu"] == "8"
    text = "\n".join(lines)
    assert "create rolebinding ostia-test-developer --role ostia-test-developer" in text
    assert "create clusterrolebinding ostia-test-developer-ostia-test" in text
    assert "--context c1" in text


def test_init_is_idempotent(fake, target, cfg):
    admin.init(target, fake, cfg=cfg)
    before = {k: v["metadata"]["uid"] for k, v in fake.store.items()}
    admin.init(target, fake, cfg=cfg)
    assert {k: v["metadata"]["uid"] for k, v in fake.store.items()} == before


def test_init_notes_what_it_could_not_detect(fake, target, cfg):
    lines = admin.init(target, fake, cfg=cfg)  # no ServiceCIDR objects in this fake cluster
    assert any("Service CIDR" in line and "not detected" in line for line in lines)


def test_init_allows_nodelocal_dns_when_it_runs(fake, target, cfg):
    fake.apply(
        {
            "apiVersion": "apps/v1",
            "kind": "DaemonSet",
            "metadata": {"name": "node-local-dns", "labels": {"k8s-app": "node-local-dns"}},
        },
        record=False,
    )
    admin.init(target, fake, cfg=cfg)
    (egress,) = [p for p in fake.list("networkpolicy") if p["metadata"]["name"] == "ostia-egress"]
    dns = egress["spec"]["egress"][0]
    # the node's DNS cache can't be selected on every CNI (GKE Dataplane V2), so any destination
    assert "to" not in dns and {"protocol": "UDP", "port": 53} in dns["ports"]


def test_init_privileged(fake, target, cfg):
    admin.init(target, fake, cfg=cfg, privileged=True)
    ns = fake.get("namespace", "ostia-test", namespaced=False)
    assert ns["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == "privileged"


PROBE_OK = [
    "PASS curl pixi exec curl works",
    "PASS metadata unreachable (expected)",
    "PASS token none mounted",
    "PASS apiserver unreachable",
    "PASS node unreachable",
    "PASS dns github.com resolves",
    "PASS https github.com answers",
    "[ostia] probe done",
]


def test_verify_reports_every_check_and_deletes_its_probe(
    fake, clock, target, cfg, tmp_path, capsys
):
    fake.script([(3, pod_phase("Running")), (6, log(PROBE_OK)), (7, pod_phase("Succeeded"))])
    assert admin.verify(target, fake, cfg=cfg, profile="cpu", clock=clock) == 0
    out = capsys.readouterr().out
    assert "metadata" in out and "expected" in out and "7 checks passed" in out
    assert fake.list("job") == []


def test_verify_fails_on_any_failed_check(fake, clock, target, cfg, capsys):
    lines = [*PROBE_OK[:-2], "FAIL https no answer from github.com", "[ostia] probe done"]
    fake.script([(3, pod_phase("Running")), (6, log(lines)), (7, pod_phase("Succeeded"))])
    assert admin.verify(target, fake, cfg=cfg, profile="cpu", clock=clock) == 1
    assert "FAIL https" in capsys.readouterr().out


def test_verify_probe_uses_the_runs_pod_spec(fake, clock, target, cfg, monkeypatch):
    seen = {}
    original = fake.apply

    def apply(obj, record=True):
        if obj["kind"] == "Job":
            seen["job"] = obj
        return original(obj, record=record)

    fake.apply = apply
    fake.script([(3, pod_phase("Running")), (6, log(PROBE_OK)), (7, pod_phase("Succeeded"))])
    admin.verify(target, fake, cfg=cfg, profile="cpu", clock=clock)
    pod = seen["job"]["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False and pod["shareProcessNamespace"] is True
    assert pod["securityContext"]["runAsUser"] == 1000
    assert seen["job"]["metadata"]["labels"]["ostia.dev/probe"] == "true"
    env = {e["name"]: e for e in pod["containers"][0]["env"]}
    assert env["OSTIA_NODE_IP"]["valueFrom"] == {"fieldRef": {"fieldPath": "status.hostIP"}}
    assert "pixi exec --spec curl" in pod["containers"][0]["command"][2]


def test_verify_a_probe_that_never_starts_is_1(fake, clock, target, cfg):
    assert admin.verify(target, fake, cfg=cfg, profile="cpu", clock=clock, timeout=60) == 1
    assert fake.list("job") == []


def _run(fake, name, owner):
    fake.apply(
        {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": f"ostia-{name}",
                "labels": {
                    "ostia.dev/managed": "true",
                    "ostia.dev/run-id": name,
                    "ostia.dev/owner": owner,
                },
            },
            "spec": {"suspend": True},
        },
        record=False,
    )


@pytest.fixture
def runs(fake):
    me = core.owner_id()
    _run(fake, "mine-1", me)
    _run(fake, "mine-2", me)
    _run(fake, "theirs", "someone")
    return fake


def _left(fake):
    return sorted(j["metadata"]["name"] for j in fake.list("job"))


def test_cleanup_defaults_to_your_runs(runs, target):
    admin.cleanup(target, runs)
    assert _left(runs) == ["ostia-theirs"]


def test_cleanup_run_id(runs, target):
    admin.cleanup(target, runs, run_id="theirs")
    assert _left(runs) == ["ostia-mine-1", "ostia-mine-2"]


def test_cleanup_all_needs_a_confirmation(runs, target):
    with pytest.raises(UsageError) as e:
        admin.cleanup(target, runs, all_=True)
    assert "--yes" in e.value.message
    admin.cleanup(target, runs, all_=True, yes=True)
    assert _left(runs) == []


def test_cleanup_cache_only_when_asked(runs, target):
    runs.apply(manifests.pvc("ostia-test", "100Gi", "x"), record=False)
    admin.cleanup(target, runs)
    assert runs.get("persistentvolumeclaim", "ostia-test-cache")
    admin.cleanup(target, runs, cache=True)
    assert runs.get("persistentvolumeclaim", "ostia-test-cache") is None


def test_delete_namespace_refuses_one_ostia_did_not_create(runs, target):
    runs.load(FIXTURES / "namespace-no-quota.json")
    with pytest.raises(UsageError) as e:
        admin.cleanup(target, runs, delete_namespace=True, yes=True)
    assert "ostia.dev/managed" in e.value.message
    assert runs.get("namespace", "ostia-test", namespaced=False)


def test_delete_namespace_with_the_managed_label(runs, target):
    runs.load(FIXTURES / "namespace-guarded.json")
    admin.cleanup(target, runs, delete_namespace=True, yes=True)
    assert runs.get("namespace", "ostia-test", namespaced=False) is None


def test_profiles_lists_the_merged_profiles(cfg):
    text = admin.profiles(cfg)
    assert "l4" in text and "nvidia-l4" in text and "a100" in text and "cpu" in text


def test_profiles_with_a_context_adds_the_fit(fake, target, cfg):
    text = admin.profiles(cfg, target=target, kube=fake)
    assert "l4 (gke)" in text and "fits gke-l4-pool-1" in text
    assert "no node matches" in text  # a100 on this cluster


def test_usage_totals_node_hours_and_cost(tmp_path, capsys):
    root = tmp_path / "remote"
    for i, (ctx, prof, hours, cost) in enumerate(
        [("c1", "l4", 0.5, 0.425), ("c1", "l4", 0.25, 0.2125), ("c1", "cpu", 1.0, None)]
    ):
        d = root / f"k8s-{prof}-{i}"
        d.mkdir(parents=True)
        (d / "summary.json").write_text(
            json.dumps(
                {
                    "backend": "k8s",
                    "context": ctx,
                    "profile": prof,
                    "node_hours": hours,
                    "cost_estimate": cost,
                }
            )
        )
    (root / "broken").mkdir()
    (root / "broken" / "summary.json").write_text("{")
    text = admin.usage(root)
    assert "c1  l4  2 runs  0.75 node-hours  $0.64" in text
    assert "c1  cpu  1 runs  1.00 node-hours  (no price)" in text
    assert "broken" in capsys.readouterr().err


def test_verify_fails_on_a_truncated_probe_log(fake, clock, target, cfg, capsys):
    fake.script([(3, pod_phase("Running")), (6, log(PROBE_OK[:2])), (7, pod_phase("Failed"))])
    assert admin.verify(target, fake, cfg=cfg, profile="cpu", clock=clock) == 1
    assert "incomplete" in capsys.readouterr().out


def test_init_on_an_existing_namespace_does_not_mark_it_managed(fake, target, cfg):
    fake.load(FIXTURES / "namespace-no-quota.json")
    admin.init(target, fake, cfg=cfg)
    labels = fake.get("namespace", "ostia-test", namespaced=False)["metadata"]["labels"]
    assert "ostia.dev/managed" not in labels  # cleanup --delete-namespace must not delete it
    assert labels["pod-security.kubernetes.io/enforce"] == "restricted"


def test_init_keeps_ipv4_cidrs_only(fake, target, cfg):
    fake.apply(
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "ServiceCIDR",
            "metadata": {"name": "kubernetes"},
            "spec": {"cidrs": ["34.118.224.0/20", "fd00:10:96::/112"]},
        },
        record=False,
    )
    admin.init(target, fake, cfg=cfg)
    (egress,) = [p for p in fake.list("networkpolicy") if p["metadata"]["name"] == "ostia-egress"]
    blocked = egress["spec"]["egress"][1]["to"][0]["ipBlock"]["except"]
    assert "34.118.224.0/20" in blocked and not [c for c in blocked if ":" in c]


def test_verify_shows_pixis_error_when_curl_is_missing(fake, clock, target, cfg, capsys):
    lines = [
        "FAIL curl pixi exec curl failed: dns error: failed to lookup address",
        "[ostia] probe done",
    ]
    fake.script([(3, pod_phase("Running")), (6, log(lines)), (7, pod_phase("Succeeded"))])
    assert admin.verify(target, fake, cfg=cfg, profile="cpu", clock=clock) == 1
    assert "dns error" in capsys.readouterr().out


def test_the_probe_passes_pixis_error_through():
    assert "err=$(pixi exec --spec curl curl --version 2>&1" in admin.PROBE
