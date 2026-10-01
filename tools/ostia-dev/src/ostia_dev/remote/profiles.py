"""Node profiles (RFC-0005 §4.4): a node type, mapped per provider."""

import re
from dataclasses import dataclass, field

from ostia_dev.config import Config
from ostia_dev.contract import violation
from ostia_dev.errors import UsageError

PROVIDERS = ("gke", "eks", "aks", "generic")
KINDS = ("gpu", "cpu", "rdma")
GIB = 1024**3


@dataclass
class Profile:
    name: str
    kind: str
    cpu: str
    memory: str
    ephemeral_storage: str
    provider: str | None = None
    gpus: int = 0
    compute_capability: str | None = None
    arch: str | None = None
    node_selector: dict[str, str] = field(default_factory=dict)
    tolerations: list[dict] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    rdma_resources: dict[str, str] = field(default_factory=dict)
    host_network: bool = False
    rdma_nics: str | None = None
    extra: dict = field(default_factory=dict)

    @property
    def is_gpu(self) -> bool:
        return self.kind in ("gpu", "rdma")

    @property
    def cc(self) -> str:
        return (self.compute_capability or "").replace(".", "")


def _bad(problem: str, details: list[str], fix: str) -> UsageError:
    return UsageError(
        violation(problem, details, "profiles map a node type per provider", fix, "RFC-0005 §4.4")
    )


def resolve(name: str, provider: str | None, cfg: Config) -> Profile:
    if name not in cfg.profiles:
        raise _bad(
            f"unknown profile {name!r}",
            [f"profiles: {', '.join(sorted(cfg.profiles))}"],
            f"pick one, or add [remote.k8s.profiles.{name}] to {cfg.path}",
        )
    raw = cfg.profiles[name]
    base = {k: v for k, v in raw.items() if k not in PROVIDERS}
    table = raw.get(provider, {}) if provider else {}
    fields = {**base, **table}
    kind = fields.get("kind", "gpu")
    if kind not in KINDS:
        raise _bad(
            f"profile {name} has kind {kind!r}",
            [f"kinds: {', '.join(KINDS)}"],
            f"set kind in [remote.k8s.profiles.{name}]",
        )
    if provider and provider not in raw and kind != "cpu":
        raise _bad(
            f"profile {name} has no mapping for provider {provider}",
            [f"mapped providers: {', '.join(p for p in PROVIDERS if p in raw) or 'none'}"],
            f"add [remote.k8s.profiles.{name}.{provider}] with a node_selector to {cfg.path}",
        )
    selector = dict(fields.get("node_selector", {}))
    if provider == "generic" and not selector:
        raise _bad(
            f"profile {name} has no node_selector for the generic provider",
            ["generic clusters have no well-known node labels to select by"],
            f'add node_selector = {{ "<label>" = "<value>" }} under '
            f"[remote.k8s.profiles.{name}.generic] in {cfg.path}",
        )
    if arch := fields.get("arch"):
        selector["kubernetes.io/arch"] = arch
    for key in ("cpu", "memory", "ephemeral_storage"):
        if key not in fields:
            raise _bad(
                f"profile {name} has no {key}", [], f"set {key} in [remote.k8s.profiles.{name}]"
            )
    for key in ("rdma_resources", "host_network", "rdma_nics"):
        if fields.get(key) and kind != "rdma":
            raise UsageError(
                violation(
                    f"profile {name} sets {key} but has kind {kind!r}",
                    [],
                    "RDMA devices and the host network are only for profiles of "
                    'kind = "rdma", which run in a privileged namespace',
                    f'set kind = "rdma" in [remote.k8s.profiles.{name}], or drop {key}',
                    "RFC-0005 §4.12",
                )
            )
    known = {
        "rdma_nics",
        "rdma_resources",
        "host_network",
        "kind",
        "cpu",
        "memory",
        "ephemeral_storage",
        "gpus",
        "compute_capability",
        "arch",
        "node_selector",
        "tolerations",
        "env",
    }
    return Profile(
        name=name,
        kind=kind,
        cpu=str(fields["cpu"]),
        memory=str(fields["memory"]),
        ephemeral_storage=str(fields["ephemeral_storage"]),
        provider=provider,
        gpus=int(fields.get("gpus", 0)),
        compute_capability=fields.get("compute_capability"),
        arch=arch,
        node_selector=selector,
        tolerations=list(fields.get("tolerations", [])),
        env=dict(fields.get("env", {})),
        rdma_resources={k: str(v) for k, v in fields.get("rdma_resources", {}).items()},
        host_network=bool(fields.get("host_network", False)),
        rdma_nics=fields.get("rdma_nics"),
        extra={k: v for k, v in fields.items() if k not in known},
    )


_QUANTITY = re.compile(r"^([0-9.]+)([A-Za-z]*)$")
_UNITS = {
    "": 1,
    "k": 10**3,
    "M": 10**6,
    "G": 10**9,
    "T": 10**12,
    "Ki": 1024,
    "Mi": 1024**2,
    "Gi": GIB,
    "Ti": 1024**4,
}


def parse_bytes(quantity: str) -> int:
    m = _QUANTITY.match(quantity)
    if not m or m.group(2) not in _UNITS:
        raise _bad(f"not a memory quantity: {quantity!r}", [], 'use Kubernetes units, e.g. "24Gi"')
    return int(float(m.group(1)) * _UNITS[m.group(2)])


def parse_cpus(quantity: str) -> float:
    q = str(quantity)
    return float(q[:-1]) / 1000 if q.endswith("m") else float(q)


def parallelism(profile: Profile, env: str) -> tuple[int, int]:
    """(build jobs, test jobs): one per CPU; CUDA builds at most one nvcc job per 4 GiB (§3.2)."""
    cpus = max(1, int(parse_cpus(profile.cpu)))
    build = cpus
    if env.startswith("cuda-"):
        build = max(1, min(cpus, parse_bytes(profile.memory) // (4 * GIB)))
    return build, cpus


_DURATION = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s?)?$")


def parse_duration(text: str | int) -> int:
    """Seconds from "90", "600s", "10m", "2h" or "1h30m"."""
    if isinstance(text, int):
        return text
    m = _DURATION.match(text.strip())
    if not text.strip() or not m:
        raise UsageError(
            violation(
                f"not a duration: {text!r}",
                [],
                "durations are a number of seconds, or use h, m and s",
                'write e.g. "90", "600s", "10m" or "1h30m"',
                "RFC-0005 §4.8",
            )
        )
    h, mins, s = (int(g) if g else 0 for g in m.groups())
    return h * 3600 + mins * 60 + s
