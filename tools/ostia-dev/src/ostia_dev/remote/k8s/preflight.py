"""The k8s preflight (RFC-0005 §4.1, §4.3, §4.4): everything here runs before any object is
created, so every refusal is exit 2 with nothing left behind.
"""

import sys
from dataclasses import dataclass, field
from pathlib import Path

from ostia_dev import config, prompts
from ostia_dev.config import Config
from ostia_dev.contract import violation
from ostia_dev.errors import OstiaError, UsageError
from ostia_dev.remote.core import RunSpec
from ostia_dev.remote.k8s import kubectl, manifests
from ostia_dev.remote.k8s.kube import KubeError
from ostia_dev.remote.profiles import Profile, parse_bytes, parse_cpus

DEFAULT_NAMESPACE = "ostia-test"
PROVIDER_LABELS = (
    ("cloud.google.com/gke-nodepool", "gke"),
    ("eks.amazonaws.com/nodegroup", "eks"),
    ("karpenter.sh/nodepool", "eks"),
    ("kubernetes.azure.com/agentpool", "aks"),
)
# The capture tool's --provider names the cloud, not the Kubernetes service (RFC-0003 §9).
CAPTURE_PROVIDERS = {"gke": "gcp", "eks": "aws", "aks": "azure", "generic": "unknown"}
INSTANCE_TYPE_LABELS = ("node.kubernetes.io/instance-type", "beta.kubernetes.io/instance-type")


@dataclass
class Target:
    context: str
    namespace: str
    provider: str
    kubectl: Path
    allow_unguarded: bool = False
    psa: str | None = None
    node_access: bool = True
    nodes: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _bad(problem: str, details: list[str], rule: str, fix: str, see: str = "RFC-0005 §4.3"):
    return UsageError(violation(problem, details, rule, fix, see))


def _table(context: str) -> str:
    return f"[remote.k8s.contexts.{config.basic_string(context)}]"


def detect_provider(nodes: list[dict]) -> str | None:
    for node in nodes[:1]:
        labels = node.get("metadata", {}).get("labels", {})
        for label, provider in PROVIDER_LABELS:
            if label in labels:
                return provider
    return None


def capture_provider(provider: str) -> str:
    return CAPTURE_PROVIDERS.get(provider, "unknown")


def instance_type(nodes: list[dict], profile: Profile) -> str:
    """The instance type of the nodes the profile selects, or "unknown" when they don't share
    one: the pod may land on any of them."""
    types = set()
    for node in nodes:
        labels = node.get("metadata", {}).get("labels", {})
        if all(labels.get(k) == v for k, v in profile.node_selector.items()):
            types.add(next((labels[k] for k in INSTANCE_TYPE_LABELS if labels.get(k)), None))
    return types.pop() if len(types) == 1 and None not in types else "unknown"


def _namespace(spec: RunSpec, ctx: dict, context: str, cfg: Config) -> str:
    if ns := spec.extra.get("namespace") or ctx.get("namespace"):
        return ns
    if not prompts.is_tty():
        raise _bad(
            f"no namespace for context {context}",
            [],
            "each run names its namespace (RFC-0005 §4.3)",
            f'pass --namespace <ns>, or add namespace = "<ns>" under {_table(context)} in '
            f"{cfg.path}",
        )
    ns = prompts.ask(f"namespace for context {context}?", DEFAULT_NAMESPACE, flag="--namespace")
    if prompts.confirm("save as this context's default?", yes=False, default=True):
        config.append_table(cfg.path, ("remote", "k8s", "contexts", context), {"namespace": ns})
    return ns


def _provider(kube, ctx: dict, context: str, cfg: Config, target: Target) -> str:
    try:
        target.nodes = kube.list("node", namespaced=False)
    except KubeError:
        target.node_access = False
        target.notes.append("no access to nodes: provider detection and the fit check skipped")
    if provider := ctx.get("provider"):
        return provider
    detected = detect_provider(target.nodes) if target.node_access else None
    if not prompts.is_tty():
        line = f'provider = "{detected or "gke|eks|aks|generic"}"'
        raise _bad(
            f"context {context} is not in the config",
            [
                f"detected provider: {detected or 'unknown'}"
                if target.node_access
                else "no access to nodes, so the provider could not be detected"
            ],
            "ostia-dev never guesses a context's provider silently (RFC-0005 §4.3)",
            f"add {_table(context)} with {line} to {cfg.path}",
        )
    if detected:
        print(f"detected provider: {detected}", file=sys.stderr)
        provider = detected
    else:
        provider = prompts.ask(
            f"provider for context {context} (gke, eks, aks, generic)?",
            "generic",
            flag="the config's provider key",
        )
    question = f'save {_table(context)} provider = "{provider}" to {cfg.path}?'
    if prompts.confirm(question, yes=False, default=True):
        config.append_table(
            cfg.path, ("remote", "k8s", "contexts", context), {"provider": provider}
        )
    return provider


def resolve(spec: RunSpec, cfg: Config, *, kube_factory, ensure_kubectl=kubectl.ensure):
    context = spec.extra.get("context")
    if not context:
        raise _bad(
            "remote k8s needs --context",
            [],
            "a run never falls back to kubectl's current context (RFC-0005 §4.1)",
            "pass --context <name>; kubectl config get-contexts lists them",
            "RFC-0005 §4.1",
        )
    ctx = cfg.context(context)
    path = ensure_kubectl(flag=spec.extra.get("kubectl"), configured=ctx.get("kubectl"))
    namespace = _namespace(spec, ctx, context, cfg)
    kube = kube_factory(path, context, namespace, provider=ctx.get("provider"))
    target = Target(
        context=context,
        namespace=namespace,
        provider="",
        kubectl=path,
        allow_unguarded=bool(spec.extra.get("allow_unguarded")),
    )
    target.provider = _provider(kube, ctx, context, cfg, target)
    kube.provider = target.provider
    return target, kube


def _detect_cidrs(kube, lines: list[str]) -> list[str]:
    """Service and pod CIDRs for the egress blocklist (P7); IPv4 only, as the ipBlock is."""
    cidrs = []
    try:
        for sc in kube.list("servicecidr", namespaced=False):
            cidrs += sc.get("spec", {}).get("cidrs", [])
    except OstiaError:
        pass
    if not cidrs:
        lines.append(
            "note: Service CIDR not detected (no ServiceCIDR API); add it to "
            "blocked_cidrs for the context if it is outside the private ranges"
        )
    try:
        for node in kube.list("node", namespaced=False):
            cidrs += node.get("spec", {}).get("podCIDRs", [])
    except OstiaError:
        lines.append("note: pod CIDRs not detected (no access to nodes)")
    return [c for c in dict.fromkeys(cidrs) if ":" not in c]


def apply_guardrails(target: Target, kube, *, cfg: Config, privileged: bool = False) -> list[str]:
    """The §4.6 guardrails, as `init` and a run's --yes both create them."""
    ctx = cfg.context(target.context)
    lines: list[str] = []
    cidrs = _detect_cidrs(kube, lines) + list(ctx.get("blocked_cidrs", []))
    try:
        nodelocal = bool(
            kube.list("daemonset", selector="k8s-app=node-local-dns", all_namespaces=True)
        )
    except OstiaError:
        nodelocal = False
    existing = kube.get("namespace", target.namespace, namespaced=False)
    objs = manifests.guardrails(
        target.namespace,
        privileged=privileged,
        blocked_cidrs=cidrs,
        nodelocal_dns=nodelocal,
        quota=ctx.get("quota"),
    )
    for obj in objs:
        if obj["kind"] == "Namespace" and existing:
            # the label means "ostia-dev created it", which lets cleanup delete it (§4.3)
            obj["metadata"]["labels"].pop("ostia.dev/managed", None)
        kube.apply(obj)
        lines.append(f"applied {obj['kind']}/{obj['metadata']['name']}")
    return lines


def _skew(target: Target, kube) -> None:
    v = kube.version()
    client, server = v.get("clientVersion", {}), v.get("serverVersion", {})
    try:
        gap = abs(int(client["minor"].rstrip("+")) - int(server["minor"].rstrip("+")))
    except (KeyError, ValueError):
        return
    if gap > 1:
        note = (
            f"warning: kubectl {client.get('gitVersion')} and the server "
            f"{server.get('gitVersion')} are more than one minor version apart; "
            'fix: a newer ostia-dev pin, or --kubectl PATH / kubectl = "…" in the config'
        )
        target.notes.append(note)
        print(note, file=sys.stderr)


def _selects(policy: dict, labels: dict) -> bool:
    sel = policy.get("spec", {}).get("podSelector", {})
    wanted = sel.get("matchLabels", {})
    return not sel.get("matchExpressions") and all(labels.get(k) == v for k, v in wanted.items())


def _ensure_namespace(target: Target, kube, *, yes: bool, cfg: Config | None) -> dict:
    ns = kube.get("namespace", target.namespace, namespaced=False)
    if ns:
        return ns
    try:
        create = prompts.confirm(f"create namespace {target.namespace}?", yes=yes)
    except UsageError as e:
        raise _bad(
            f"namespace {target.namespace} does not exist",
            [f"context: {target.context}"],
            "runs need a namespace with the guardrails of RFC-0005 §4.6",
            f"ostia-dev remote k8s init --context {target.context} --namespace "
            f"{target.namespace}, or rerun with --yes to create it",
        ) from e
    if not create:
        raise _bad(
            f"namespace {target.namespace} does not exist",
            [],
            "runs need a namespace",
            "rerun and answer yes, or run init",
        )
    apply_guardrails(target, kube, cfg=cfg or config.load(), privileged=False)
    return kube.get("namespace", target.namespace, namespaced=False)


def check(
    target: Target, kube, profile: Profile, *, yes: bool, cfg: Config | None = None
) -> list[str]:
    _skew(target, kube)
    ns = _ensure_namespace(target, kube, yes=yes, cfg=cfg)
    target.psa = ns["metadata"].get("labels", {}).get("pod-security.kubernetes.io/enforce")
    run_labels = {"ostia.dev/managed": "true"}
    missing = []
    if not target.psa:
        missing.append("a Pod Security enforce label")
    if not kube.list("resourcequota"):
        missing.append("a ResourceQuota")
    if not any(_selects(p, run_labels) for p in kube.list("networkpolicy")):
        missing.append("a NetworkPolicy selecting the run's pods")
    if missing and not target.allow_unguarded:
        what = ", no ".join(m.removeprefix("a ") for m in missing)
        raise _bad(
            f"namespace {target.namespace} has no {what}",
            [f"context: {target.context}", *[f"missing: {m}" for m in missing]],
            "runs need the guardrails of RFC-0005 §4.6 (PSA restricted, ResourceQuota, "
            "NetworkPolicy)",
            f"ostia-dev remote k8s init --context {target.context} --namespace "
            f"{target.namespace}\n       or rerun with --allow-unguarded "
            "(recorded in the run summary)",
        )
    for m in missing:
        target.notes.append(f"unguarded: {m} is missing (--allow-unguarded)")
    if profile.kind == "rdma" and target.psa != "privileged":
        raise _bad(
            f"profile {profile.name} is an rdma profile, and namespace {target.namespace} is "
            f"{target.psa or 'not privileged'}",
            [],
            "RDMA pods run only in a PSA privileged namespace (RFC-0005 §4.12)",
            f"ostia-dev remote k8s init --privileged --context {target.context} --namespace <ns>",
            "RFC-0005 §4.12",
        )
    if not kube.get("serviceaccount", manifests.SERVICE_ACCOUNT):
        raise _bad(
            f"the ServiceAccount {manifests.SERVICE_ACCOUNT} is missing in {target.namespace}",
            [],
            "runs use a ServiceAccount with no RBAC bindings (RFC-0005 §4.6)",
            f"ostia-dev remote k8s init --context {target.context} --namespace {target.namespace}",
        )
    if target.node_access and (note := fit_check(target.nodes, profile)):
        target.notes.append(note)
    return target.notes


def _fits(alloc: dict, profile: Profile) -> list[str]:
    short = []
    if parse_cpus(alloc.get("cpu", "0")) < parse_cpus(profile.cpu):
        short.append("cpu")
    if parse_bytes(alloc.get("memory", "0")) < parse_bytes(profile.memory):
        short.append("memory")
    if parse_bytes(alloc.get("ephemeral-storage", "0")) < parse_bytes(profile.ephemeral_storage):
        short.append("ephemeral_storage")
    if profile.gpus and int(alloc.get("nvidia.com/gpu", "0")) < profile.gpus:
        short.append("gpus")
    return short


def fit_check(nodes: list[dict], profile: Profile) -> str | None:
    """None when a matching node fits; a note when none matches yet (a pool may scale up)."""
    matching = [
        n
        for n in nodes
        if all(
            n["metadata"].get("labels", {}).get(k) == v for k, v in profile.node_selector.items()
        )
    ]
    if not matching:
        return (
            f"no node matches profile {profile.name}'s selector now; an autoscaler may add "
            "one, so the fit check is skipped"
        )
    shortfalls = [(n, _fits(n["status"].get("allocatable", {}), profile)) for n in matching]
    if any(not s for _, s in shortfalls):
        return None
    node, short = min(shortfalls, key=lambda ns: len(ns[1]))
    alloc = node["status"]["allocatable"]
    shape = ", ".join(
        f"{k} {alloc.get(k, '?')}"
        for k in ("cpu", "memory", "ephemeral-storage", "nvidia.com/gpu")
        if k in alloc
    )
    raise UsageError(
        violation(
            f"profile {profile.name} fits no node that matches its selector",
            [
                f"largest shape: {node['metadata']['name']}: {shape}",
                f"profile requests: cpu {profile.cpu}, memory {profile.memory}, "
                f"ephemeral_storage {profile.ephemeral_storage}, gpus {profile.gpus}",
            ],
            "a pod gets exactly what its profile requests (RFC-0005 §4.4)",
            f"lower {', '.join(short)} in [remote.k8s.profiles.{profile.name}]",
            "RFC-0005 §4.4",
        )
    )
