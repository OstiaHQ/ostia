#!/usr/bin/env python3
"""Capability profiles of the reference setups (RFC-0001 §6.4, RFC-0004 §1).

    capabilities.py [--setups infra/setups]

Each setup file (`infra/setups/<setup>.yaml`, the format of RFC-0004 §1) names its gate
workloads, its also-run workloads, and a primary and a fallback machine with their
capabilities. This check runs before anything is rented: a gate workload that a machine
cannot support fails, and an also-run workload it cannot support is reported
`unsupported`. `rent` (RFC-0004, Rollout PR 7) confirms the capabilities with active
probes on the machine itself.

Capabilities: nvlink-p2p, cuda-ipc, gpudirect-rdma, multi-rail (two or more RDMA NICs
per node), tcp, efa.
"""

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = 1


@dataclass(frozen=True)
class Needs:
    capabilities: tuple[str, ...] = ()
    min_gpus: int = 0
    nodes: int = 1


# What each workload needs from one machine; `nodes` is a minimum. A workload with several
# forms uses the one with the most nodes the machine has: dual_link is two RDMA rails on a
# multi-node machine, and two NVLink paths (a source and two peer GPUs) on a single node.
WORKLOADS: dict[str, list[Needs]] = {
    "p2p_copy": [Needs(("nvlink-p2p",), min_gpus=2)],
    "pipelining": [Needs(("nvlink-p2p",), min_gpus=2)],
    "batching": [Needs(("nvlink-p2p",), min_gpus=2)],
    "dual_link": [
        Needs(("gpudirect-rdma", "multi-rail"), min_gpus=1, nodes=2),
        Needs(("nvlink-p2p",), min_gpus=3, nodes=1),
    ],
    "rdma_put": [Needs(("gpudirect-rdma",), min_gpus=1, nodes=2)],
    "gdr_stream": [Needs(("gpudirect-rdma",), min_gpus=1, nodes=2)],
    "tcp_put": [Needs(("tcp",), nodes=2)],
    "onpath_placement": [Needs(("nvlink-p2p",), min_gpus=2)],
    "topo_capture": [Needs()],
}
MACHINE_FIELDS = ("backend", "purchase", "nodes", "accelerators", "capabilities")
BACKENDS = ("skypilot", "azure", "k8s")
# A k8s machine names a logical machine the user config maps to a cluster (RFC-0005 §6);
# it has pods instead of nodes, and nothing to buy.
K8S_FIELDS = ("backend", "machine", "pods", "accelerators", "capabilities")
K8S_FORBIDDEN = ("purchase", "cloud", "region", "instance_type", "nodes")


class SetupError(ValueError):
    pass


@dataclass
class Result:
    workload: str
    required: bool
    supported: bool
    reason: str = ""


def load_setup(path: Path) -> dict:
    try:
        setup = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as e:
        raise SetupError(f"{path}: {e}") from e
    if not isinstance(setup, dict) or setup.get("schema") != SCHEMA:
        raise SetupError(f"{path}: schema must be {SCHEMA}")
    if setup.get("name") != path.stem:
        raise SetupError(f"{path}: name '{setup.get('name')}' does not match the file name")
    for key in ("gate", "also_run"):
        for w in setup.get(key) or []:
            if w not in WORKLOADS:
                raise SetupError(f"{path}: unknown workload '{w}' in {key}")
    machines = setup.get("machines") or {}
    if set(machines) != {"primary", "fallback"}:
        raise SetupError(f"{path}: machines must be exactly primary and fallback")
    for name, m in machines.items():
        if m.get("backend") not in BACKENDS:
            raise SetupError(
                f"{path}: machine {name} has backend '{m.get('backend')}'; "
                f"use one of {', '.join(BACKENDS)}"
            )
        k8s = m["backend"] == "k8s"
        missing = [f for f in (K8S_FIELDS if k8s else MACHINE_FIELDS) if f not in m]
        if missing:
            raise SetupError(f"{path}: machine {name} is missing {', '.join(missing)}")
        if k8s:
            extra = [f for f in K8S_FORBIDDEN if f in m]
            if extra:
                raise SetupError(
                    f"{path}: k8s machine {name} cannot set {', '.join(extra)} "
                    "(pods sets the node count; nothing is bought)"
                )
            if m["pods"] not in (1, 2):
                raise SetupError(f"{path}: k8s machine {name}: pods must be 1 or 2")
            m["nodes"] = m["pods"]
    setup["gate"] = setup.get("gate") or []
    setup["also_run"] = setup.get("also_run") or []
    return setup


def _gpus(machine: dict) -> int:
    _, _, count = str(machine["accelerators"]).rpartition(":")
    return int(count) if count.isdigit() else 0


def _why_not(needs: Needs, machine: dict) -> str:
    missing = [c for c in needs.capabilities if c not in machine["capabilities"]]
    if missing:
        return "missing " + ", ".join(missing)
    if machine["nodes"] < needs.nodes:
        return f"needs {needs.nodes} nodes, has {machine['nodes']}"
    if _gpus(machine) < needs.min_gpus:
        return f"needs {needs.min_gpus} GPUs per node, has {_gpus(machine)}"
    return ""


def evaluate(setup: dict) -> dict[str, list[Result]]:
    out = {}
    for name, machine in setup["machines"].items():
        results = []
        for w in setup["gate"] + setup["also_run"]:
            forms = WORKLOADS[w]
            form = next((n for n in forms if n.nodes <= machine["nodes"]), forms[-1])
            reason = _why_not(form, machine)
            results.append(Result(w, w in setup["gate"], not reason, reason))
        out[name] = results
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--setups", type=Path, default=ROOT / "infra" / "setups")
    args = parser.parse_args(argv)
    failed = 0
    for path in sorted(args.setups.glob("*.yaml")):
        try:
            setup = load_setup(path)
        except SetupError as e:
            print(f"error: {e}\n  see: RFC-0004 §1")
            failed += 1
            continue
        for machine, results in evaluate(setup).items():
            for r in results:
                where = f"{setup['name']}/{machine}"
                if r.supported:
                    print(f"{where}: {r.workload} supported")
                elif r.required:
                    failed += 1
                    print(
                        f"error: {where} cannot run gate workload {r.workload} ({r.reason})\n"
                        "  rule: a gate workload the machine cannot support fails the gate\n"
                        f"  fix: choose a machine with the capability in {path.name}\n"
                        "  see: RFC-0001 §6.4"
                    )
                else:
                    print(f"{where}: {r.workload} unsupported ({r.reason}); also-run only")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
