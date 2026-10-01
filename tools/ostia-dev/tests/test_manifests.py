"""The Job, the per-run objects and the namespace guardrails (RFC-0005 §4.2, §4.4, §4.6-§4.8)."""

import datetime
import os
from pathlib import Path

import pytest
import yaml
from fakes.runs import make_run
from ostia_dev import config
from ostia_dev.errors import UsageError
from ostia_dev.remote.k8s import manifests

GOLDEN = Path(__file__).parent / "golden" / "manifests"
NOW = datetime.datetime(2026, 10, 2, 14, 15, 1, tzinfo=datetime.UTC)
OWNER = "0123456789ab"
SCRIPT = "# ostia-supervisor (golden stand-in)\nexit 0\n"
GENERIC = """schema = 1
[remote.k8s.profiles.l4.generic]
node_selector = { "example.com/gpu" = "l4" }
[remote.k8s.profiles.a100.generic]
node_selector = { "example.com/gpu" = "a100" }
[remote.k8s.profiles.cpu.generic]
node_selector = { "kubernetes.io/os" = "linux" }
"""


def _golden(name: str, objs) -> None:
    text = manifests.dump(objs if isinstance(objs, list) else [objs])
    path = GOLDEN / f"{name}.yaml"
    if os.environ.get("OSTIA_UPDATE_GOLDEN"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    assert text == path.read_text(), f"golden {path.name} differs; OSTIA_UPDATE_GOLDEN=1 updates it"


def _cfg(tmp_path):
    path = tmp_path / "generic.toml"
    path.write_text(GENERIC)
    return config.load(path)


def _job(run, **kw):
    return manifests.job(run, namespace="ostia-test", script=SCRIPT, owner=OWNER, now=NOW, **kw)


CASES = [
    (prov, prof)
    for prov in ("gke", "eks", "aks", "generic")
    for prof in ("l4", "cpu", "a100")
    if (prov, prof) != ("aks", "l4")
]


@pytest.mark.parametrize(("provider", "profile"), CASES, ids=[f"{a}-{b}" for a, b in CASES])
def test_golden_jobs(tmp_path, provider, profile):
    cfg = _cfg(tmp_path) if provider == "generic" else None
    _golden(f"job-{provider}-{profile}", _job(make_run(tmp_path, profile, provider, cfg=cfg)))


def test_l4_has_no_aks_mapping(tmp_path):
    with pytest.raises(UsageError):
        make_run(tmp_path, "l4", "aks")


def test_golden_job_with_cache_secret_and_keep(tmp_path):
    run = make_run(tmp_path, "cpu", "gke", env_vars={"GH_TOKEN": "x"})
    _golden(
        "job-gke-cpu-cache-secret-keep",
        _job(
            run,
            cache=True,
            secret="ostia-k8s-l4-20261002-141501-a1b2c3-env",
            secret_keys={"GH_TOKEN"},
            keep=1800,
        ),
    )


def test_golden_secret_and_pvc():
    owner = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "name": "ostia-r1",
        "uid": "UID",
        "controller": True,
        "blockOwnerDeletion": True,
    }
    _golden("secret", manifests.secret("r1", {"GH_TOKEN": "x"}, owner, OWNER))
    _golden("pvc", manifests.pvc("ostia-test", "100Gi", OWNER))


@pytest.mark.parametrize(
    ("name", "kw"),
    [
        ("guardrails-default", {}),
        ("guardrails-privileged", {"privileged": True}),
        (
            "guardrails-nodelocal-cidrs",
            {"nodelocal_dns": True, "blocked_cidrs": ["34.118.224.0/20", "10.52.0.0/14"]},
        ),
    ],
)
def test_golden_guardrails(name, kw):
    _golden(name, manifests.guardrails("ostia-test", **kw))


def test_deadline_is_the_sum_of_the_windows(tmp_path):
    run = make_run(tmp_path, "cpu", "gke")
    assert manifests.deadline_seconds(run.windows) == 115 * 60  # 20 + 10 + 60 + 10 + 15
    assert manifests.deadline_seconds(run.windows, keep=1800) == 145 * 60
    job = _job(run)
    assert job["spec"]["activeDeadlineSeconds"] == 115 * 60
    assert job["spec"]["ttlSecondsAfterFinished"] == 600


def test_job_is_suspended_and_never_retried(tmp_path):
    spec = _job(make_run(tmp_path, "cpu", "gke"))["spec"]
    assert (spec["suspend"], spec["backoffLimit"]) == (True, 0)
    assert spec["template"]["spec"]["restartPolicy"] == "Never"


def test_pod_is_restricted_compatible(tmp_path):
    pod = _job(make_run(tmp_path))["spec"]["template"]["spec"]
    assert pod["securityContext"] == {
        "runAsNonRoot": True,
        "runAsUser": 1000,
        "runAsGroup": 1000,
        "fsGroup": 1000,
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    c = pod["containers"][0]
    assert c["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
        "runAsNonRoot": True,
    }
    assert not {"hostNetwork", "hostPID", "hostIPC"} & set(pod)
    assert all("hostPath" not in v for v in pod["volumes"])


def test_no_identity_and_a_shared_process_namespace(tmp_path):
    pod = _job(make_run(tmp_path))["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "ostia-test-runner"
    assert pod["automountServiceAccountToken"] is False
    assert pod["shareProcessNamespace"] is True


def test_requests_equal_limits_and_gpus_only_on_gpu_profiles(tmp_path):
    gpu = _job(make_run(tmp_path, "l4"))["spec"]["template"]["spec"]["containers"][0]["resources"]
    cpu = _job(make_run(tmp_path, "cpu", "gke"))["spec"]["template"]["spec"]["containers"][0]
    assert gpu["requests"] == gpu["limits"]
    assert gpu["limits"] == {
        "cpu": "6",
        "memory": "24Gi",
        "ephemeral-storage": "60Gi",
        "nvidia.com/gpu": "1",
    }
    assert "nvidia.com/gpu" not in cpu["resources"]["limits"]


def test_emptydir_size_limit_is_ephemeral_storage(tmp_path):
    pod = _job(make_run(tmp_path))["spec"]["template"]["spec"]
    (work,) = [v for v in pod["volumes"] if v["name"] == "work"]
    assert work["emptyDir"] == {"sizeLimit": "60Gi"}
    assert {"name": "work", "mountPath": "/w"} in pod["containers"][0]["volumeMounts"]


def test_the_supervisor_argv_and_image(tmp_path):
    run = make_run(tmp_path)
    c = _job(run)["spec"]["template"]["spec"]["containers"][0]
    assert c["command"] == ["/bin/sh", "-c", SCRIPT, "ostia-supervisor"]
    assert c["image"] == run.image


def _env(job) -> dict:
    return {
        e["name"]: e.get("value") for e in job["spec"]["template"]["spec"]["containers"][0]["env"]
    }


def test_profile_env_with_a_reference_is_expanded_by_the_supervisor(tmp_path):
    env = _env(_job(make_run(tmp_path, "l4", "gke")))
    assert env["LD_LIBRARY_PATH"] == "/usr/local/nvidia/lib64"
    assert env["OSTIA_EXPAND_ENV"] == "PATH=/usr/local/nvidia/bin:$PATH"
    assert "PATH" not in env
    assert "$" in env["OSTIA_PLAN"]  # the plan itself is never expanded


def test_keep_widens_the_collection_window(tmp_path):
    run = make_run(tmp_path, "cpu", "gke")
    assert _env(_job(run))["OSTIA_COLLECT_WINDOW"] == "600"
    assert _env(_job(run, keep=1800))["OSTIA_COLLECT_WINDOW"] == "2400"


def test_cache_mounts_the_pvc_and_points_the_caches_at_it(tmp_path):
    job = _job(make_run(tmp_path, "cpu", "gke"), cache=True)
    pod = job["spec"]["template"]["spec"]
    assert {"name": "cache", "persistentVolumeClaim": {"claimName": "ostia-test-cache"}} in pod[
        "volumes"
    ]
    env = _env(job)
    assert (env["RATTLER_CACHE_DIR"], env["CPM_SOURCE_CACHE"], env["CCACHE_DIR"]) == (
        "/w/cache/rattler",
        "/w/cache/cpm",
        "/w/cache/ccache",
    )


def test_secret_values_never_in_the_job(tmp_path):
    run = make_run(tmp_path, "cpu", "gke", env_vars={"GH_TOKEN": "s3cret", "MODE": "fast"})
    job = _job(run, secret="ostia-r-env", secret_keys={"GH_TOKEN"})
    assert "s3cret" not in manifests.dump([job])
    c = job["spec"]["template"]["spec"]["containers"][0]
    assert c["envFrom"] == [{"secretRef": {"name": "ostia-r-env"}}]
    assert _env(job)["MODE"] == "fast"  # a value that isn't secret stays plain env


def test_labels_and_expiry(tmp_path):
    job = _job(make_run(tmp_path, "cpu", "gke"))
    labels = job["metadata"]["labels"]
    assert labels == {
        "ostia.dev/managed": "true",
        "ostia.dev/run-id": "k8s-l4-20261002-141501-a1b2c3",
        "ostia.dev/owner": OWNER,
    }
    assert job["spec"]["template"]["metadata"]["labels"] == labels
    assert job["metadata"]["annotations"] == {"ostia.dev/expires": "2026-10-02T16:10:01Z"}


def test_egress_blocklist_always_has_the_private_ranges():
    (egress,) = [o for o in manifests.guardrails("n") if o["metadata"]["name"] == "ostia-egress"]
    https = egress["spec"]["egress"][1]
    assert https["ports"] == [{"protocol": "TCP", "port": 443}]
    assert set(https["to"][0]["ipBlock"]["except"]) >= {
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "100.64.0.0/10",
        "169.254.0.0/16",
    }


def test_role_rules_match_rfc_4_3_plus_secrets():
    (role,) = [o for o in manifests.guardrails("n") if o["kind"] == "Role"]
    rules = {
        (g, r): set(rule["verbs"])
        for rule in role["rules"]
        for g in rule["apiGroups"]
        for r in rule["resources"]
    }
    assert rules[("batch", "jobs")] == {"create", "get", "list", "watch", "patch", "delete"}
    assert rules[("", "pods")] == {"get", "list", "watch", "delete"}
    assert rules[("", "pods/exec")] == {"create"}
    assert rules[("", "pods/log")] == {"get"}
    assert rules[("", "services")] == {"create", "get", "delete"}
    # preflight lists the policies to find one selecting the run's pods (RFC §4.3 update note)
    assert rules[("networking.k8s.io", "networkpolicies")] == {"create", "get", "list", "delete"}
    assert (
        rules[("", "events")]
        == rules[("", "resourcequotas")]
        == rules[("", "limitranges")]
        == {"get", "list"}
    )
    assert rules[("", "persistentvolumeclaims")] == {"create", "get", "delete"}
    assert rules[("", "secrets")] == {"create", "get", "delete"}  # the per-run Secret (§4.10)
    assert rules[("", "serviceaccounts")] == {"get"}


def test_dump_writes_block_scalars_for_multiline_strings():
    text = manifests.dump([{"a": "one\ntwo\n"}])
    assert "a: |" in text and yaml.safe_load(text) == {"a": "one\ntwo\n"}


def test_dns_is_kube_dns_only_without_nodelocal():
    (egress,) = [o for o in manifests.guardrails("n") if o["metadata"]["name"] == "ostia-egress"]
    dns = egress["spec"]["egress"][0]
    assert dns["to"] == [
        {
            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
            "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
        }
    ]


def test_dns_goes_anywhere_with_nodelocal_but_https_stays_blocked():
    objs = manifests.guardrails("n", nodelocal_dns=True)
    (egress,) = [o for o in objs if o["metadata"]["name"] == "ostia-egress"]
    dns, https = egress["spec"]["egress"]
    assert "to" not in dns and sorted(p["protocol"] for p in dns["ports"]) == ["TCP", "UDP"]
    assert {p["port"] for p in dns["ports"]} == {53}
    assert "169.254.0.0/16" in https["to"][0]["ipBlock"]["except"]


OWNER_REF = {
    "apiVersion": "batch/v1",
    "kind": "Job",
    "name": "ostia-k8s-l4-20261002-141501-a1b2c3",
    "uid": "11111111-2222-3333-4444-555555555555",
    "controller": True,
    "blockOwnerDeletion": True,
}


def _env_of(job):
    return {e["name"]: e for e in job["spec"]["template"]["spec"]["containers"][0]["env"]}


def test_golden_two_pod_objects(tmp_path):
    run = make_run(tmp_path, "l4", "gke")
    _golden("job-indexed-gke-l4", _job(run, pods=2))
    cpu = make_run(tmp_path, "cpu", "generic", cfg=_cfg(tmp_path))
    _golden("job-indexed-same-node-generic-cpu", _job(cpu, pods=2, same_node=True))
    name = manifests.job_name(run.run_id)
    _golden("service-two-pod", manifests.service(run.run_id, name, OWNER_REF, OWNER))
    _golden("run-policy-two-pod", manifests.run_policy(run.run_id, OWNER_REF, OWNER))


def test_two_pod_job_shape(tmp_path):
    run = make_run(tmp_path, "l4", "gke")
    job = _job(run, pods=2)
    spec, name = job["spec"], job["metadata"]["name"]
    assert (spec["completionMode"], spec["completions"], spec["parallelism"]) == ("Indexed", 2, 2)
    pod = spec["template"]["spec"]
    assert pod["subdomain"] == name
    [term] = pod["affinity"]["podAntiAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]
    assert term["labelSelector"]["matchLabels"] == {"ostia.dev/run-id": run.run_id}
    assert term["topologyKey"] == "kubernetes.io/hostname"
    env = _env_of(job)
    assert env["OSTIA_SIZE"]["value"] == "2" and env["OSTIA_PORT"]["value"] == "29400"
    assert env["OSTIA_PEER_HOST"]["value"] == f"{name}-0.{name}"
    field = env["OSTIA_RANK"]["valueFrom"]["fieldRef"]["fieldPath"]
    assert field == "metadata.annotations['batch.kubernetes.io/job-completion-index']"


def test_same_node_drops_anti_affinity(tmp_path):
    job = _job(make_run(tmp_path, "l4", "gke"), pods=2, same_node=True)
    assert "affinity" not in job["spec"]["template"]["spec"]


def test_two_pod_code_wait_and_deadline(tmp_path):
    run = make_run(tmp_path, "l4", "gke")
    assert _env_of(_job(run))["OSTIA_CODE_WAIT"]["value"] == "600"
    job = _job(run, pods=2)
    assert _env_of(job)["OSTIA_CODE_WAIT"]["value"] == "1200"
    assert job["spec"]["activeDeadlineSeconds"] == (20 + 20 + 60 + 10 + 15) * 60
    assert manifests.deadline_seconds(run.windows, pods=2) == 7500
    assert manifests.deadline_seconds(run.windows) == 6900


def test_service_and_policy_shape(tmp_path):
    run = make_run(tmp_path, "l4", "gke")
    name = manifests.job_name(run.run_id)
    svc = manifests.service(run.run_id, name, OWNER_REF, OWNER)
    assert svc["metadata"]["name"] == name and svc["spec"]["clusterIP"] == "None"
    assert svc["spec"]["publishNotReadyAddresses"] is True
    assert svc["spec"]["selector"] == {"ostia.dev/run-id": run.run_id}
    assert svc["spec"]["ports"] == [{"name": "rendezvous", "port": 29400, "protocol": "TCP"}]
    assert svc["metadata"]["ownerReferences"] == [OWNER_REF]
    pol = manifests.run_policy(run.run_id, OWNER_REF, OWNER)
    peers = [{"podSelector": {"matchLabels": {"ostia.dev/run-id": run.run_id}}}]
    assert pol["spec"]["ingress"] == [{"from": peers}]
    assert pol["spec"]["egress"] == [{"to": peers}]
    assert pol["metadata"]["ownerReferences"] == [OWNER_REF]


def test_service_name_is_a_dns_label(tmp_path):
    import re

    longest = max(config.load(tmp_path / "none.toml").profiles, key=len)
    run_id = f"k8s-{longest}-20261002-141501-a1b2c3"
    host = f"{manifests.job_name(run_id)}-1"
    assert len(host) <= 63 and re.fullmatch(r"[a-z]([-a-z0-9]*[a-z0-9])?", host)


def test_long_profile_name_is_refused_for_two_pods(tmp_path):
    run = make_run(tmp_path, "l4", "gke", run_id="k8s-" + "x" * 40 + "-20261002-141501-a1b2c3")
    with pytest.raises(UsageError) as e:
        _job(run, pods=2)
    assert "63" in e.value.message and "profile" in e.value.message
