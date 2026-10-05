"""Capture directories as ostia-topo-capture writes them (RFC-0003 §4), for the fetch tests."""

import hashlib
import json

TOPO1 = "topo1:sha256:" + "ab" * 32
NIC_A = "0000:11:00.0"
NIC_B = "0000:12:00.0"


def nics(link_layer: str = "infiniband") -> dict:
    """Two NICs in bus order, mlx5_0 on the first and mlx5_1 on the second."""
    nic = {"driver": "mlx5_core", "link_layer": link_layer, "max_pcie_gen": 4,
           "max_pcie_width": 16, "numa_node": 0, "port_speed_mbps": 200000}  # fmt: skip
    port = {"port": 1, "state": "active", "link_layer": link_layer, "active_speed": 200000,
            "active_width": 4, "gpudirect": "dmabuf"}  # fmt: skip
    return {
        "schema": 1,
        "rdma_probe": "ok",
        "nics": [{"bus_id": NIC_A, **nic}, {"bus_id": NIC_B, **nic}],
        "rdma": [
            {"bus_id": NIC_B, "device": "mlx5_1", **port},
            {"bus_id": NIC_A, "device": "mlx5_0", **port},
        ],
    }


def capture_files(
    status: str = "complete", *, sanitized: bool = True, nics_doc: dict | None = None, **manifest
) -> dict[str, bytes]:
    """manifest.json, the data files it lists with their real hashes, and diagnostics.txt."""
    data = {
        "hwloc.xml": b"<topology/>\n",
        "nics.json": (json.dumps(nics_doc or nics()) + "\n").encode(),
        "meta.json": b'{"schema": 1, "provider": "gcp", "instance_type": "a2-highgpu-1g"}\n',
    }
    if status == "complete":
        data["nvml.json"] = b'{"gpus": []}\n'
    doc = {
        "schema": 1,
        "tool_version": "0.1.0",
        "status": status,
        "sanitized": sanitized,
        "leak_check": "passed",
        "topology_id": TOPO1,
        "files": {n: "sha256:" + hashlib.sha256(b).hexdigest() for n, b in data.items()},
        "missing": [] if status == "complete" else [{"file": "nvml.json", "reason": "nvml_init"}],
        "errors": [],
    }
    doc.update(manifest)
    return {
        **data,
        "manifest.json": json.dumps(doc).encode(),
        "diagnostics.txt": b"hwloc: 4 PCI device(s)\n",
    }
