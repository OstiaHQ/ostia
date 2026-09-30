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
    assert (
        rules[("", "services")]
        == rules[("networking.k8s.io", "networkpolicies")]
        == {"create", "get", "delete"}
    )
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
