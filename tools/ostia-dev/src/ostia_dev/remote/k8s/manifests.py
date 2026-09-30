"""The Job, its per-run objects and the namespace guardrails (RFC-0005 §4.2, §4.6-§4.8)."""

import datetime

import yaml

from ostia_dev.remote.core import Run
from ostia_dev.remote.suites import WORK

SERVICE_ACCOUNT = "ostia-test-runner"
ROLE = "ostia-test-developer"
CACHE_PVC = "ostia-test-cache"
PRIVATE_RANGES = [
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "100.64.0.0/10",
    "169.254.0.0/16",
]
NODELOCAL_DNS = "169.254.20.10/32"
QUOTA = {
    "requests.nvidia.com/gpu": "4",
    "limits.cpu": "64",
    "limits.memory": "256Gi",
    "limits.ephemeral-storage": "1Ti",
    "pods": "10",
}
LIMIT_DEFAULTS = {"cpu": "1", "memory": "2Gi", "ephemeral-storage": "10Gi"}
CACHE_ENV = {
    "RATTLER_CACHE_DIR": f"{WORK}/cache/rattler",
    "CPM_SOURCE_CACHE": f"{WORK}/cache/cpm",
    "CCACHE_DIR": f"{WORK}/cache/ccache",
}


def labels(run_id: str, owner: str) -> dict:
    return {"ostia.dev/managed": "true", "ostia.dev/run-id": run_id, "ostia.dev/owner": owner}


def _stamp(t: datetime.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def deadline_seconds(windows: dict[str, int], keep: int = 0) -> int:
    """activeDeadlineSeconds counts Pending time too, so it is the sum of every phase (§4.8)."""
    return (
        windows["schedule_timeout"]
        + windows["code_wait"]
        + windows["timeout"]
        + windows["collect"]
        + keep
        + windows["margin"]
    )


def job_name(run_id: str) -> str:
    return f"ostia-{run_id}"


def _env(run: Run, keep: int, cache: bool, secret_keys: set[str]) -> list[dict]:
    env = run.supervisor_env()
    # Kubernetes never expands $VAR from the image's environment; the supervisor does.
    expand = {k: v for k, v in run.profile.env.items() if "$" in v and env.get(k) == v}
    for k in expand:
        del env[k]
    if expand:
        env["OSTIA_EXPAND_ENV"] = "\n".join(f"{k}={v}" for k, v in expand.items())
    env["OSTIA_COLLECT_WINDOW"] = str(run.windows["collect"] + keep)
    if cache:
        env.update(CACHE_ENV)
    for k in secret_keys:
        env.pop(k, None)  # secret values reach the pod only from the per-run Secret
    return [{"name": k, "value": v} for k, v in env.items()]


def job(
    run: Run,
    *,
    namespace: str,
    script: str,
    owner: str,
    now: datetime.datetime,
    keep: int = 0,
    cache: bool = False,
    secret: str | None = None,
    secret_keys: frozenset[str] | set[str] = frozenset(),
) -> dict:
    p = run.profile
    deadline = deadline_seconds(run.windows, keep)
    resources = {"cpu": p.cpu, "memory": p.memory, "ephemeral-storage": p.ephemeral_storage}
    if p.gpus:
        resources["nvidia.com/gpu"] = str(p.gpus)
    container = {
        "name": "supervisor",
        "image": run.image,
        "command": ["/bin/sh", "-c", script, "ostia-supervisor"],
        "env": _env(run, keep, cache, secret_keys),
        "resources": {"requests": dict(resources), "limits": dict(resources)},
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]},
            "runAsNonRoot": True,
        },
        "volumeMounts": [{"name": "work", "mountPath": WORK}],
    }
    volumes = [{"name": "work", "emptyDir": {"sizeLimit": p.ephemeral_storage}}]
    if secret:
        container["envFrom"] = [{"secretRef": {"name": secret}}]
    if cache:
        container["volumeMounts"].append({"name": "cache", "mountPath": f"{WORK}/cache"})
        volumes.append({"name": "cache", "persistentVolumeClaim": {"claimName": CACHE_PVC}})
    pod = {
        "restartPolicy": "Never",
        "serviceAccountName": SERVICE_ACCOUNT,
        "automountServiceAccountToken": False,
        "shareProcessNamespace": True,
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": 1000,
            "runAsGroup": 1000,
            "fsGroup": 1000,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "containers": [container],
        "volumes": volumes,
    }
    if p.node_selector:
        pod["nodeSelector"] = dict(p.node_selector)
    if p.tolerations:
        pod["tolerations"] = list(p.tolerations)
    meta_labels = labels(run.run_id, owner)
    expires = now + datetime.timedelta(seconds=deadline)
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": job_name(run.run_id),
            "namespace": namespace,
            "labels": meta_labels,
            "annotations": {"ostia.dev/expires": _stamp(expires)},
        },
        "spec": {
            "suspend": True,
            "backoffLimit": 0,
            "activeDeadlineSeconds": deadline,
            "ttlSecondsAfterFinished": run.windows["ttl"],
            "template": {"metadata": {"labels": dict(meta_labels)}, "spec": pod},
        },
    }


def owner_reference(job_obj: dict) -> dict:
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "name": job_obj["metadata"]["name"],
        "uid": job_obj["metadata"]["uid"],
        "controller": True,
        "blockOwnerDeletion": True,
    }


def secret(run_id: str, values: dict[str, str], owner_ref: dict, owner: str) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "type": "Opaque",
        "metadata": {
            "name": f"{job_name(run_id)}-env",
            "labels": labels(run_id, owner),
            "ownerReferences": [owner_ref],
        },
        "stringData": dict(values),
    }


def pvc(namespace: str, size: str, owner: str) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": CACHE_PVC,
            "namespace": namespace,
            "labels": {"ostia.dev/managed": "true", "ostia.dev/owner": owner},
        },
        "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": size}}},
    }


def _rule(groups, resources, verbs, names=None) -> dict:
    rule = {"apiGroups": groups, "resources": resources, "verbs": verbs}
    if names:
        rule["resourceNames"] = names
    return rule


def guardrails(
    namespace: str,
    *,
    privileged: bool = False,
    blocked_cidrs=(),
    nodelocal_dns: bool = False,
    quota: dict | None = None,
) -> list[dict]:
    level = "privileged" if privileged else "restricted"
    managed = {"ostia.dev/managed": "true"}
    dns_to = [
        {
            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
            "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
        }
    ]
    if nodelocal_dns:
        dns_to.append({"ipBlock": {"cidr": NODELOCAL_DNS}})
    blocked = PRIVATE_RANGES + [c for c in blocked_cidrs if c not in PRIVATE_RANGES]
    meta = lambda name: {"name": name, "namespace": namespace, "labels": dict(managed)}  # noqa: E731
    return [
        {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {
                "name": namespace,
                "labels": {
                    **managed,
                    **{
                        f"pod-security.kubernetes.io/{m}": level
                        for m in ("enforce", "warn", "audit")
                    },
                },
            },
        },
        {
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": meta(SERVICE_ACCOUNT),
            "automountServiceAccountToken": False,
        },
        {
            "apiVersion": "v1",
            "kind": "ResourceQuota",
            "metadata": meta("ostia-test-quota"),
            "spec": {"hard": {**QUOTA, **(quota or {})}},
        },
        {
            "apiVersion": "v1",
            "kind": "LimitRange",
            "metadata": meta("ostia-test-limits"),
            "spec": {
                "limits": [
                    {
                        "type": "Container",
                        "default": dict(LIMIT_DEFAULTS),
                        "defaultRequest": dict(LIMIT_DEFAULTS),
                    }
                ]
            },
        },
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": meta("ostia-default-deny"),
            "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]},
        },
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": meta("ostia-egress"),
            "spec": {
                "podSelector": {},
                "policyTypes": ["Egress"],
                "egress": [
                    {
                        "to": dns_to,
                        "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}],
                    },
                    {
                        "to": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": blocked}}],
                        "ports": [{"protocol": "TCP", "port": 443}],
                    },
                ],
            },
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "Role",
            "metadata": meta(ROLE),
            "rules": [
                _rule(["batch"], ["jobs"], ["create", "get", "list", "watch", "patch", "delete"]),
                _rule([""], ["pods"], ["get", "list", "watch", "delete"]),
                _rule([""], ["pods/exec"], ["create"]),
                _rule([""], ["pods/log"], ["get"]),
                _rule([""], ["services"], ["create", "get", "delete"]),
                _rule(
                    ["networking.k8s.io"], ["networkpolicies"], ["create", "get", "list", "delete"]
                ),
                _rule([""], ["events", "resourcequotas", "limitranges"], ["get", "list"]),
                _rule([""], ["persistentvolumeclaims"], ["create", "get", "delete"]),
                _rule([""], ["secrets"], ["create", "get", "delete"]),
                _rule([""], ["serviceaccounts"], ["get"], [SERVICE_ACCOUNT]),
            ],
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRole",
            "metadata": {"name": f"{ROLE}-{namespace}", "labels": dict(managed)},
            "rules": [
                _rule([""], ["namespaces"], ["get"], [namespace]),
                _rule([""], ["nodes"], ["get", "list"]),
            ],
        },
    ]


class _Dumper(yaml.SafeDumper):
    pass


def _str(dumper, value: str):
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_Dumper.add_representer(str, _str)


def dump(objs: list[dict]) -> str:
    return yaml.dump_all(objs, Dumper=_Dumper, sort_keys=False, width=1000, allow_unicode=True)
