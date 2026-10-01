"""The Job, its per-run objects and the namespace guardrails (RFC-0005 §4.2, §4.6-§4.8)."""

import datetime

import yaml

from ostia_dev.errors import UsageError
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
QUOTA = {
    "requests.nvidia.com/gpu": "4",
    "limits.cpu": "64",
    "limits.memory": "256Gi",
    "limits.ephemeral-storage": "1Ti",
    "pods": "10",
}
LIMIT_DEFAULTS = {"cpu": "1", "memory": "2Gi", "ephemeral-storage": "10Gi"}
RENDEZVOUS_PORT = 29400
INDEX = "batch.kubernetes.io/job-completion-index"
CACHE_ENV = {
    "RATTLER_CACHE_DIR": f"{WORK}/cache/rattler",
    "CPM_SOURCE_CACHE": f"{WORK}/cache/cpm",
    "CCACHE_DIR": f"{WORK}/cache/ccache",
}


def labels(run_id: str, owner: str) -> dict:
    return {"ostia.dev/managed": "true", "ostia.dev/run-id": run_id, "ostia.dev/owner": owner}


def _stamp(t: datetime.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def code_wait(windows: dict[str, int], pods: int = 1) -> int:
    """A two-pod run's first pod may wait for the second's scale-up (§4.11)."""
    if pods > 1:
        return max(windows["code_wait"], windows["schedule_timeout"])
    return windows["code_wait"]


def deadline_seconds(windows: dict[str, int], keep: int = 0, pods: int = 1) -> int:
    """activeDeadlineSeconds counts Pending time too, so it is the sum of every phase (§4.8)."""
    return (
        windows["schedule_timeout"]
        + code_wait(windows, pods)
        + windows["timeout"]
        + windows["collect"]
        + keep
        + windows["margin"]
    )


def job_name(run_id: str) -> str:
    return f"ostia-{run_id}"


def _env(run: Run, keep: int, cache: bool, secret_keys: set[str], pods: int = 1) -> list[dict]:
    env = run.supervisor_env()
    env["OSTIA_CODE_WAIT"] = str(code_wait(run.windows, pods))
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
    items = [{"name": k, "value": v} for k, v in env.items()]
    if pods > 1:
        name = job_name(run.run_id)
        items += [
            {
                "name": "OSTIA_RANK",
                "valueFrom": {"fieldRef": {"fieldPath": f"metadata.annotations['{INDEX}']"}},
            },
            {"name": "OSTIA_SIZE", "value": str(pods)},
            {"name": "OSTIA_PORT", "value": str(RENDEZVOUS_PORT)},
            {"name": "OSTIA_PEER_HOST", "value": f"{name}-0.{name}"},
        ]
    return items


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
    pods: int = 1,
    same_node: bool = False,
) -> dict:
    p = run.profile
    deadline = deadline_seconds(run.windows, keep, pods)
    name = job_name(run.run_id)
    if pods > 1 and len(f"{name}-{pods - 1}") > 63:
        raise UsageError(
            f"error: pod hostname {name}-{pods - 1} is longer than 63 characters\n"
            "  rule: two-pod runs reach rank 0 at <job>-0.<job>, a DNS label of at most 63\n"
            f"  fix: use a profile with a shorter name than {p.name!r}\n"
            "  see: RFC-0005 §4.11"
        )
    resources = {"cpu": p.cpu, "memory": p.memory, "ephemeral-storage": p.ephemeral_storage}
    if p.gpus:
        resources["nvidia.com/gpu"] = str(p.gpus)
    container = {
        "name": "supervisor",
        "image": run.image,
        "command": ["/bin/sh", "-c", script, "ostia-supervisor"],
        "env": _env(run, keep, cache, secret_keys, pods),
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
    if pods > 1:
        pod["subdomain"] = name
        if not same_node:
            pod["affinity"] = {
                "podAntiAffinity": {
                    "requiredDuringSchedulingIgnoredDuringExecution": [
                        {
                            "labelSelector": {"matchLabels": {"ostia.dev/run-id": run.run_id}},
                            "topologyKey": "kubernetes.io/hostname",
                        }
                    ]
                }
            }
    meta_labels = labels(run.run_id, owner)
    expires = now + datetime.timedelta(seconds=deadline)
    spec = {
        "suspend": True,
        "backoffLimit": 0,
        "activeDeadlineSeconds": deadline,
        "ttlSecondsAfterFinished": run.windows["ttl"],
        "template": {"metadata": {"labels": dict(meta_labels)}, "spec": pod},
    }
    if pods > 1:
        spec.update(completionMode="Indexed", completions=pods, parallelism=pods)
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": job_name(run.run_id),
            "namespace": namespace,
            "labels": meta_labels,
            "annotations": {"ostia.dev/expires": _stamp(expires)},
        },
        "spec": spec,
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


def service(run_id: str, name: str, owner_ref: dict, owner: str) -> dict:
    """The headless Service that gives rank 0 its DNS name before it is Ready (§4.11)."""
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name, "labels": labels(run_id, owner), "ownerReferences": [owner_ref]},
        "spec": {
            "clusterIP": "None",
            "publishNotReadyAddresses": True,
            "selector": {"ostia.dev/run-id": run_id},
            "ports": [{"name": "rendezvous", "port": RENDEZVOUS_PORT, "protocol": "TCP"}],
        },
    }


def run_policy(run_id: str, owner_ref: dict, owner: str) -> dict:
    """Traffic between a run's own pods, on every port: UCX opens more after the rendezvous."""

    def peers() -> list[dict]:
        return [{"podSelector": {"matchLabels": {"ostia.dev/run-id": run_id}}}]

    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {
            "name": f"{job_name(run_id)}-peers",
            "labels": labels(run_id, owner),
            "ownerReferences": [owner_ref],
        },
        "spec": {
            "podSelector": {"matchLabels": {"ostia.dev/run-id": run_id}},
            "policyTypes": ["Ingress", "Egress"],
            "ingress": [{"from": peers()}],
            "egress": [{"to": peers()}],
        },
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
    dns = {"ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]}
    if not nodelocal_dns:
        dns["to"] = [
            {
                "namespaceSelector": {
                    "matchLabels": {"kubernetes.io/metadata.name": "kube-system"}
                },
                "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
            }
        ]
    # With NodeLocal DNSCache the answers come from a host-network pod, which neither a pod
    # selector nor an ipBlock matches on every CNI (GKE Dataplane V2 drops about a third of
    # the lookups), so DNS may go to any destination; HTTPS and the metadata API stay blocked.
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
                    dns,
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
