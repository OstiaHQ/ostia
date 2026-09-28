#!/usr/bin/env python3
"""Evidence of the transport a gate workload actually used (RFC-0001 §6.4).

    evidence.py bench/results/<run id>/evidence/<workload>.json

A gate run that cannot show it used the capability it tests fails instead of passing.
`ostia_bench.py run --evidence` writes one evidence file per workload:
- the UCX lanes of the measuring rank's endpoint (`ucp_ep_print_info`);
- the facts the program prints as `evidence: key=value` lines (memory type of the
  registration, peer access, paths, bytes moved);
- the traffic each InfiniBand port (sysfs `port_{xmit,rcv}_data`, 4-byte units) and
  each NVLink (`nvidia-smi nvlink -gt d`) carried during the run.

Traffic thresholds: the counters must account for at least half of the bytes moved; a
two-path run needs a quarter of them on each of two NICs, or on each of two peer GPUs.
The counters include other traffic on the box, which rented gate machines do not have.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

SCHEMA = 1
IB_TLS = ("rc_mlx5", "dc_mlx5", "rc_verbs", "ud_mlx5", "ud_verbs")
IB_ROOT = Path("/sys/class/infiniband")
LANE = re.compile(r"lane\[\d+\]:\s+\d+:([A-Za-z0-9_]+)/(\S+)")
NVLINK_GPU = re.compile(r"^GPU (\d+):")
NVLINK_DATA = re.compile(r"Link (\d+): Data [TR]x: (\d+) KiB")
NVLINK_WORKLOADS = ("p2p_copy", "pipelining", "batching")
RDMA_WORKLOADS = ("rdma_put", "gdr_stream")


def ucx_lanes(text: str) -> list[dict]:
    lanes = []
    for tl, device in LANE.findall(text):
        lane = {"tl": tl, "device": re.sub(r"\.\d+$", "", device)}
        if lane not in lanes:
            lanes.append(lane)
    return lanes


def ib_counters(root: Path = IB_ROOT) -> dict[str, int]:
    """Bytes sent plus received so far on every InfiniBand port."""
    out = {}
    for port in sorted(root.glob("*/ports/*")):
        c = port / "counters"
        try:
            words = int((c / "port_xmit_data").read_text()) + int((c / "port_rcv_data").read_text())
        except (OSError, ValueError):
            continue
        out[f"{port.parent.parent.name}:{port.name}"] = words * 4
    return out


def nvlink_counters(text: str) -> dict[str, int]:
    """Bytes sent plus received so far on every NVLink, as "<gpu>/<link>"."""
    out: dict[str, int] = {}
    gpu = None
    for line in text.splitlines():
        if m := NVLINK_GPU.match(line):
            gpu = m.group(1)
        elif (m := NVLINK_DATA.search(line)) and gpu is not None:
            key = f"{gpu}/{m.group(1)}"
            out[key] = out.get(key, 0) + int(m.group(2)) * 1024
    return out


def snapshot() -> dict:
    nvlink = {}
    if shutil.which("nvidia-smi"):
        r = subprocess.run(
            ["nvidia-smi", "nvlink", "-gt", "d"], capture_output=True, text=True, check=False
        )
        nvlink = nvlink_counters(r.stdout)
    return {"nic": ib_counters(), "nvlink": nvlink}


def program_facts(text: str) -> dict[str, str]:
    facts = {}
    for line in text.splitlines():
        if line.startswith("evidence:"):
            for item in line.removeprefix("evidence:").split():
                key, _, value = item.partition("=")
                facts[key] = value
    return facts


def _delta(before: dict, after: dict) -> dict[str, int]:
    return {k: v - before.get(k, 0) for k, v in after.items() if v - before.get(k, 0) > 0}


def build(workload: str, before: dict, after: dict, output: str) -> dict:
    return {
        "schema": SCHEMA,
        "workload": workload,
        "lanes": ucx_lanes(output),
        "program": program_facts(output),
        "nic_bytes": _delta(before["nic"], after["nic"]),
        "nvlink_bytes": _delta(before["nvlink"], after["nvlink"]),
    }


def _per_gpu(nvlink: dict[str, int]) -> dict[str, int]:
    out: dict[str, int] = {}
    for key, n in nvlink.items():
        gpu = key.split("/")[0]
        out[gpu] = out.get(gpu, 0) + n
    return out


def check(ev: dict) -> list[str]:
    """Problems with the evidence; empty when it shows the capability under test."""
    w, facts, lanes = ev["workload"], ev["program"], ev["lanes"]
    moved = int(facts.get("bytes", 0))
    problems = []
    mode = facts.get("mode", "rails" if w in RDMA_WORKLOADS else "nvlink")
    if w in RDMA_WORKLOADS or (w == "dual_link" and mode == "rails"):
        ib = sorted({lane["device"] for lane in lanes if lane["tl"] in IB_TLS})
        if not ib:
            problems.append(f"no InfiniBand lane in the endpoint (lanes: {lanes})")
        if facts.get("memory_type") != "cuda":
            problems.append(f"registered memory type is {facts.get('memory_type')}, not cuda")
        carried = sorted(n for n in ev["nic_bytes"].values())
        if w == "dual_link":
            if len([n for n in carried if n >= moved / 4]) < 2:
                problems.append(f"traffic did not use two NICs (per-NIC bytes: {ev['nic_bytes']})")
        elif sum(carried) < moved / 2 or not carried:
            problems.append(f"NIC traffic {sum(carried)} B is below half of {moved} B moved")
    elif w in NVLINK_WORKLOADS or w == "dual_link":
        if facts.get("peer_access") != "1":
            problems.append("the program did not report peer access between the GPUs")
        total = sum(ev["nvlink_bytes"].values())
        if w == "dual_link":
            src = facts.get("src", "0")
            peers = {g: n for g, n in _per_gpu(ev["nvlink_bytes"]).items() if g != src}
            if len([n for n in peers.values() if n >= moved / 4]) < 2:
                problems.append(f"traffic did not use two NVLink paths (per GPU: {peers})")
        elif total < moved / 2 or total == 0:
            problems.append(f"NVLink traffic {total} B is below half of {moved} B moved")
    elif w == "tcp_put":
        if not any(lane["tl"] == "tcp" for lane in lanes):
            problems.append(f"no tcp lane in the endpoint (lanes: {lanes})")
    else:
        problems.append(f"no evidence rule for workload '{w}'")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="+", type=Path)
    args = parser.parse_args(argv)
    failed = 0
    for path in args.files:
        try:
            ev = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            print(f"error: {path}: no readable evidence ({e})\n  see: RFC-0001 §6.4")
            failed += 1
            continue
        problems = check(ev)
        if problems:
            failed += 1
            print(f"error: {ev['workload']}: the run does not show the transport it tests")
            for p in problems:
                print(f"  {p}")
            print("  rule: a gate run that cannot show its transport fails\n  see: RFC-0001 §6.4")
        else:
            print(f"{ev['workload']}: evidence ok")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
