#!/usr/bin/env python3
"""Generate the synthetic topology fixtures (RFC-0003 §9); stdlib only.

Every value is fictional. The generated files are committed, and the
fabric.topo.fixtures_current ctest runs `generate.py --check` so the two
cannot drift apart. expected.json is hand-reviewed, never generated.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import quoteattr

HERE = Path(__file__).resolve().parent

# hwloc exports PCIe bandwidth in GB/s: transfer rate (GT/s) with 128b/130b coding, per lane.
GEN_GTS = {3: 8.0, 4: 16.0, 5: 32.0}

GIB = 1 << 30
NOT_GENERATED = {"expected.json"}


@dataclass
class Gpu:
    name: str
    cc: tuple[int, int]
    mem: int
    pci_id: str
    gen: int = 4
    width: int = 16
    nvlink_version: int = 3
    bus: str = ""
    # (state, remote_type, remote_bus) per link, or the string "not_supported".
    nvlinks: list[tuple[str, str, str]] | str = field(default_factory=list)
    query: str = "ok"


@dataclass
class Nic:
    driver: str
    link_layer: str
    speed: int | None
    gen: int
    width: int
    pci_id: str
    pci_class: str = "0207"
    bus: str = ""
    numa: int = 0


@dataclass
class Bridge:
    # numa is the NUMA node whose subtree owns the host bridge.
    numa: int
    bus_range: str
    devices: list[str]
    # Bus ID of a PCI-to-PCI bridge between the host bridge and devices, if any.
    upstream: str | None = None


@dataclass
class Machine:
    packages: int
    numa_per_package: int
    pus_per_numa: int
    disallowed_pus: set[int] = field(default_factory=set)
    bridges: list[Bridge] = field(default_factory=list)
    gpus: list[Gpu] = field(default_factory=list)
    nics: list[Nic] = field(default_factory=list)
    links: list[dict] = field(default_factory=list)

    @property
    def pus(self) -> int:
        return self.packages * self.numa_per_package * self.pus_per_numa


def cpuset(pus) -> str:
    mask = 0
    for p in pus:
        mask |= 1 << p
    groups = []
    while mask:
        groups.append(mask & 0xFFFFFFFF)
        mask >>= 32
    if not groups:
        groups = [0]
    return ",".join(f"0x{g:08x}" for g in reversed(groups))


def link_speed(gen: int, width: int) -> str:
    return f"{GEN_GTS[gen] * 128 / 130 / 8 * width:.6f}"


class XmlWriter:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.gp = 0
        self.depth = 0

    def next_gp(self) -> int:
        self.gp += 1
        return self.gp

    def open(self, tag: str, attrs: dict, selfclose: bool = False) -> None:
        text = " ".join(f"{k}={quoteattr(str(v))}" for k, v in attrs.items())
        end = "/>" if selfclose else ">"
        self.lines.append("  " * self.depth + f"<{tag} {text}{end}")
        if not selfclose:
            self.depth += 1

    def close(self, tag: str) -> None:
        self.depth -= 1
        self.lines.append("  " * self.depth + f"</{tag}>")


def bus_parts(bus: str) -> tuple[int, int]:
    return int(bus[5:7], 16), int(bus[8:10], 16)


def pci_object(w: XmlWriter, bus: str, pci_type: str, gen: int, width: int) -> None:
    w.open(
        "object",
        {
            "type": "PCIDev",
            "gp_index": w.next_gp(),
            "pci_busid": bus,
            "pci_type": pci_type,
            "pci_link_speed": link_speed(gen, width),
        },
    )
    w.open("info", {"name": "OstiaPCIeMaxGen", "value": gen}, True)
    w.open("info", {"name": "OstiaPCIeMaxWidth", "value": width}, True)
    w.close("object")


def hwloc_xml(m: Machine) -> str:
    devices = {g.bus: (g.pci_id, "0302", g.gen, g.width) for g in m.gpus}
    devices.update({n.bus: (n.pci_id, n.pci_class, n.gen, n.width) for n in m.nics})

    def sets(pus) -> dict:
        return {
            "cpuset": cpuset(pus),
            "complete_cpuset": cpuset(pus),
        }

    numa_total = m.packages * m.numa_per_package
    all_pus = range(m.pus)
    allowed = [p for p in all_pus if p not in m.disallowed_pus]

    w = XmlWriter()
    w.lines += [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<!DOCTYPE topology SYSTEM "hwloc2.dtd">',
        '<topology version="2.0">',
    ]
    w.depth = 1

    def nodeset(nodes) -> dict:
        return {"nodeset": cpuset(nodes), "complete_nodeset": cpuset(nodes)}

    w.open(
        "object",
        {
            "type": "Machine",
            "os_index": 0,
            **sets(all_pus),
            "allowed_cpuset": cpuset(allowed),
            **nodeset(range(numa_total)),
            "allowed_nodeset": cpuset(range(numa_total)),
            "gp_index": w.next_gp(),
        },
    )

    def emit_bridges(numa: int) -> None:
        for br in m.bridges:
            if br.numa != numa:
                continue
            lo, hi = br.bus_range.split(":")[1].strip("[]").split("-")
            w.open(
                "object",
                {
                    "type": "Bridge",
                    "gp_index": w.next_gp(),
                    "bridge_type": "0-1",
                    "depth": 0,
                    "bridge_pci": f"0000:[{lo}-{hi}]",
                },
            )
            if br.upstream:
                secondary = f"{int(lo, 16) + 1:02x}"
                w.open(
                    "object",
                    {
                        "type": "Bridge",
                        "gp_index": w.next_gp(),
                        "bridge_type": "1-1",
                        "depth": 1,
                        "bridge_pci": f"0000:[{secondary}-{secondary}]",
                        "pci_busid": br.upstream,
                        "pci_type": "0604 [10b5:9797] [0000:0000] a1",
                        "pci_link_speed": link_speed(5, 16),
                    },
                )
                w.open("info", {"name": "OstiaPCIeMaxGen", "value": 5}, True)
                w.open("info", {"name": "OstiaPCIeMaxWidth", "value": 16}, True)
            for bus in br.devices:
                pci_id, cls, gen, width = devices[bus]
                vendor, device = pci_id.split(":")
                pci_object(w, bus, f"{cls} [{vendor}:{device}] [{vendor}:0000] a1", gen, width)
            if br.upstream:
                w.close("object")
            w.close("object")

    def emit_numa_subtree(numa: int, wrapper: bool) -> None:
        first = numa * m.pus_per_numa
        pus = range(first, first + m.pus_per_numa)
        attrs = {**sets(pus), **nodeset([numa])}
        if wrapper:
            w.open(
                "object",
                {
                    "type": "L3Cache",
                    **attrs,
                    "gp_index": w.next_gp(),
                    "cache_size": 33554432,
                    "depth": 3,
                    "cache_linesize": 64,
                    "cache_associativity": 16,
                    "cache_type": 0,
                },
            )
        w.open(
            "object",
            {
                "type": "NUMANode",
                "os_index": numa,
                **attrs,
                "gp_index": w.next_gp(),
                "local_memory": 64 * GIB,
            },
            True,
        )
        for p in pus:
            one = {**sets([p]), **nodeset([numa])}
            w.open("object", {"type": "Core", "os_index": p, **one, "gp_index": w.next_gp()})
            w.open("object", {"type": "PU", "os_index": p, **one, "gp_index": w.next_gp()}, True)
            w.close("object")
        emit_bridges(numa)
        if wrapper:
            w.close("object")

    for pkg in range(m.packages):
        numas = range(pkg * m.numa_per_package, (pkg + 1) * m.numa_per_package)
        pus = range(numas[0] * m.pus_per_numa, (numas[-1] + 1) * m.pus_per_numa)
        w.open(
            "object",
            {
                "type": "Package",
                "os_index": pkg,
                **sets(pus),
                **nodeset(numas),
                "gp_index": w.next_gp(),
            },
        )
        for n in numas:
            emit_numa_subtree(n, m.numa_per_package > 1)
        w.close("object")
    w.close("object")
    w.lines.append("</topology>")
    return "\n".join(w.lines) + "\n"


def dump(obj) -> str:
    return json.dumps(obj, indent=2, sort_keys=True) + "\n"


def nvml_json(m: Machine) -> str:
    gpus = []
    for i, g in enumerate(m.gpus):
        if isinstance(g.nvlinks, str):
            links = g.nvlinks
        else:
            links = []
            for n, (state, remote_type, remote) in enumerate(g.nvlinks):
                link = {
                    "link": n,
                    "state": state,
                    "version": g.nvlink_version,
                    "remote_type": remote_type,
                }
                if remote:
                    link["remote_bus_id"] = remote
                links.append(link)
        gpus.append(
            {
                "bus_id": g.bus,
                "name": g.name,
                "cc_major": g.cc[0],
                "cc_minor": g.cc[1],
                "memory_bytes": g.mem,
                "cuda_ordinal": i,
                "nvlinks": links,
            }
        )
    p2p = []
    for i, a in enumerate(m.gpus):
        for b in m.gpus[i + 1 :]:
            linked = (
                any(r == b.bus for _, _, r in a.nvlinks) if isinstance(a.nvlinks, list) else False
            )
            switched = isinstance(a.nvlinks, list) and any(t == "switch" for _, t, _ in a.nvlinks)
            if a.query == "unknown":
                state = nvl = "unknown"
            else:
                state = "ok"
                nvl = "ok" if linked or switched else "not_supported"
            p2p.append(
                {
                    "a": a.bus,
                    "b": b.bus,
                    "read": state,
                    "write": state,
                    "nvlink": nvl,
                    "atomics": state,
                }
            )
    return dump({"schema": 1, "gpus": gpus, "p2p": p2p})


def nics_json(m: Machine) -> str:
    nics = []
    rdma = []
    for i, n in enumerate(m.nics):
        nics.append(
            {
                "bus_id": n.bus,
                "driver": n.driver,
                "link_layer": n.link_layer,
                "max_pcie_gen": n.gen,
                "max_pcie_width": n.width,
                "numa_node": n.numa,
                "port_speed_mbps": n.speed if n.speed is not None else "unknown",
            }
        )
        if n.link_layer == "infiniband":
            rdma.append(
                {
                    "bus_id": n.bus,
                    "device": f"mlx5_{i}",
                    "port": 1,
                    "state": "ACTIVE",
                    "link_layer": "infiniband",
                    "active_speed": "NDR",
                    "active_width": "4x",
                    "gpudirect": "nvidia_peermem",
                }
            )
    return dump(
        {"schema": 1, "rdma_probe": "ok" if rdma else "unavailable", "nics": nics, "rdma": rdma}
    )


def machine_files(m: Machine) -> dict[str, str]:
    files = {"hwloc.xml": hwloc_xml(m), "nics.json": nics_json(m)}
    if m.gpus:
        files["nvml.json"] = nvml_json(m)
    if m.links:
        files["links.json"] = dump({"schema": 1, "links": m.links})
    return files


def place(m: Machine, groups: list[tuple[int, int, list, bool]]) -> None:
    """Assign bus IDs and build host bridges.

    Each group is (numa, first_bus, devices, behind_pci_bridge). Devices on a PCI bridge share its
    secondary bus; the rest sit on the host bridge's first bus.
    """
    for numa, base, devs, switched in groups:
        bus = base + (2 if switched else 1)
        ids = []
        for slot, dev in enumerate(devs):
            dev.bus = f"0000:{bus:02x}:{slot:02x}.0"
            if isinstance(dev, Nic):
                dev.numa = numa
            ids.append(dev.bus)
        m.bridges.append(
            Bridge(
                numa,
                f"0000:[{base:02x}-{base + 0x0F:02x}]",
                ids,
                f"0000:{base + 1:02x}:00.0" if switched else None,
            )
        )


def mesh(gpus: list[Gpu], per_pair: int, inactive: tuple[int, int] | None = None) -> None:
    for i, g in enumerate(gpus):
        g.nvlinks = []
        for j, peer in enumerate(gpus):
            if i == j:
                continue
            for n in range(per_pair):
                down = inactive == (i, j) and n == 0
                g.nvlinks.append(("inactive" if down else "active", "gpu", peer.bus))


def h100() -> Gpu:
    return Gpu("NVIDIA H100 80GB HBM3", (9, 0), 80 * GIB, "10de:2330", 5, 16, 4)


def a100() -> Gpu:
    return Gpu("NVIDIA A100-SXM4-80GB", (8, 0), 80 * GIB, "10de:20b2", 4, 16, 3)


def l4() -> Gpu:
    return Gpu("NVIDIA L4", (8, 9), 24 * GIB, "10de:27b8", 4, 16, 4, nvlinks="not_supported")


def ib_nic() -> Nic:
    return Nic("mlx5_core", "infiniband", 400000, 5, 16, "15b3:1021")


def eth_nic(speed: int | None = 100000) -> Nic:
    return Nic("mlx5_core", "ethernet", speed, 4, 16, "15b3:101d", "0200")


def case_nvswitch_hidden() -> dict[str, str]:
    m = Machine(2, 1, 8)
    m.gpus = [h100() for _ in range(8)]
    m.nics = [ib_nic() for _ in range(4)]
    place(
        m,
        [
            (i // 2, 0x10 * (i + 1), [m.gpus[2 * i], m.gpus[2 * i + 1], m.nics[i]], True)
            for i in range(4)
        ],
    )
    for g in m.gpus:
        g.nvlinks = [("active", "switch", "") for _ in range(18)]
    return machine_files(m)


def case_broken_nvlink() -> dict[str, str]:
    m = Machine(1, 1, 8)
    m.gpus = [a100() for _ in range(4)]
    m.nics = [ib_nic()]
    place(m, [(0, 0x20, m.gpus + m.nics, False)])
    mesh(m.gpus, 4, inactive=(0, 1))
    return machine_files(m)


def case_no_nic() -> dict[str, str]:
    m = Machine(1, 1, 4)
    m.gpus = [l4()]
    place(m, [(0, 0x30, m.gpus, False)])
    return machine_files(m)


def case_multi_numa() -> dict[str, str]:
    m = Machine(2, 2, 16)
    m.gpus = [a100() for _ in range(4)]
    m.nics = [ib_nic(), ib_nic()]
    place(
        m,
        [
            (0, 0x10, [m.gpus[0], m.nics[0]], False),
            (1, 0x30, [m.gpus[1]], False),
            (2, 0x50, [m.gpus[2], m.nics[1]], False),
            (3, 0x70, [m.gpus[3]], False),
        ],
    )
    for a, b in ((0, 1), (2, 3)):
        pair = [m.gpus[a], m.gpus[b]]
        mesh(pair, 12)
    return machine_files(m)


def case_asymmetric_links() -> dict[str, str]:
    m = Machine(1, 1, 8)
    m.gpus = [h100(), h100()]
    place(m, [(0, 0x10, m.gpus, False)])
    mesh(m.gpus, 6)
    test = {"bench": "p2p_copy", "bytes": 268435456, "concurrency": 1}
    a, b = m.gpus[0].bus, m.gpus[1].bus
    m.links = [
        {
            "from": a,
            "to": b,
            "kind": "nvlink",
            "direction": "forward",
            "bw_mbps": 230000,
            "latency_ns": 1800,
            "test": test,
            "samples": 20,
        },
        {
            "from": a,
            "to": b,
            "kind": "nvlink",
            "direction": "reverse",
            "bw_mbps": 190000,
            "latency_ns": 1900,
            "test": test,
            "samples": 20,
        },
    ]
    return machine_files(m)


def case_partial_discovery() -> dict[str, str]:
    m = Machine(1, 1, 8)
    m.gpus = [a100(), a100()]
    place(m, [(0, 0x10, m.gpus, False)])
    for g in m.gpus:
        g.nvlinks = "not_supported"
        g.query = "unknown"
    m.nics = []
    return machine_files(m)


def case_disallowed_pu() -> dict[str, str]:
    m = Machine(1, 1, 4, disallowed_pus={2, 3})
    m.gpus = [l4()]
    m.nics = [eth_nic()]
    place(m, [(0, 0x10, m.gpus + m.nics, False)])
    return machine_files(m)


def case_unknown_port() -> dict[str, str]:
    m = Machine(1, 1, 4)
    m.nics = [eth_nic(None), eth_nic(None)]
    for n in m.nics:
        n.link_layer = "unknown"
    place(m, [(0, 0x10, m.nics, False)])
    return machine_files(m)


def tcp_node() -> dict[str, str]:
    m = Machine(1, 1, 4)
    m.gpus = [l4()]
    m.nics = [eth_nic(25000)]
    place(m, [(0, 0x10, m.gpus + m.nics, False)])
    return machine_files(m)


def case_pair_tcp() -> dict[str, str]:
    files = {}
    for node in ("node-0", "node-1"):
        for name, text in tcp_node().items():
            files[f"{node}/{name}"] = text
    files["pair.json"] = dump(
        {
            "schema": 1,
            "nodes": ["node-0", "node-1"],
            "link_class": "tcp",
            "rails": [{"node-0": {"nic_index": 0}, "node-1": {"nic_index": 0}}],
            "measured": [
                {"rail": 0, "direction": "forward", "bw_mbps": 2900, "test": "tcp-stream"}
            ],
        }
    )
    return files


CASES = {
    "nvswitch-hidden": case_nvswitch_hidden,
    "broken-nvlink": case_broken_nvlink,
    "no-nic": case_no_nic,
    "multi-numa": case_multi_numa,
    "asymmetric-links": case_asymmetric_links,
    "partial-discovery": case_partial_discovery,
    "disallowed-pu": case_disallowed_pu,
    "unknown-port": case_unknown_port,
    "pair-tcp": case_pair_tcp,
}


def generate(out: Path) -> None:
    for case, build in CASES.items():
        for rel, text in build().items():
            path = out / case / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")


def files_under(root: Path) -> set[str]:
    return {
        str(p.relative_to(root))
        for p in root.rglob("*")
        if p.is_file() and p.name not in NOT_GENERATED and p.parent != root
    }


def check(committed: Path) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        fresh = Path(tmp)
        generate(fresh)
        want = files_under(fresh)
        have = {f for f in files_under(committed) if f.split("/")[0] in CASES}
        drift = sorted(
            f
            for f in want | have
            if f not in want
            or f not in have
            or (fresh / f).read_bytes() != (committed / f).read_bytes()
        )
    for f in drift:
        print(f"drift: {f}")
    return 1 if drift else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="fail if committed files differ")
    ap.add_argument("--out", type=Path, default=HERE, help="fixture root (default: this directory)")
    args = ap.parse_args()
    if args.check:
        return check(args.out)
    generate(args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
