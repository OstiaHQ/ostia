"""Benchmark records to links.json (RFC-0003 §8).

Only unidirectional `p2p_copy` records become links. Each names its endpoints by PCI bus ID in
the record's `devices` field, so CUDA ordinals are never mapped. A link's kind is `nvlink` when
the capture's nvml.json shows an active NVLink between the two GPUs, directly or through
NVSwitches, and `pcie` otherwise. Records of the same (from, to, bytes) pool their samples into
one entry.
"""

import json
import re
import statistics
from collections.abc import Iterable
from pathlib import Path

from ostia_dev import errors
from ostia_dev.contract import violation

BUS_ID = re.compile(r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]")
# p2p_copy reports GB/s (1e9 bytes per second); links.json holds MB/s.
UNIT = "GB/s"


def _bad_record(index: int, problem: str, rule: str) -> errors.UsageError:
    return errors.UsageError(
        violation(
            f"record {index}: {problem}",
            [],
            rule,
            "convert the JSONL of a run whose benchmarks write `devices` "
            "(pixi run ostia-dev bench run)",
            "RFC-0003 §8",
        )
    )


def _bad_capture(problem: str, fix: str) -> errors.UsageError:
    return errors.UsageError(
        violation(
            problem,
            [],
            "a link's kind comes from the NVLinks in the capture of the measured machine",
            fix,
            "RFC-0003 §8",
        )
    )


def _gpus_and_nvlinks(capture: Path) -> tuple[set[str], set[tuple[str, str]]]:
    path = capture / "nvml.json"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        gpus = doc["gpus"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise _bad_capture(
            "the capture has no readable nvml.json",
            "pass --capture <dir> of a complete capture of the machine that ran the benchmarks",
        ) from e
    buses: set[str] = set()
    direct: set[tuple[str, str]] = set()
    switched: set[str] = set()
    for gpu in gpus:
        bus = str(gpu.get("bus_id", "")).lower()
        buses.add(bus)
        links = gpu.get("nvlinks")
        for link in links if isinstance(links, list) else []:
            if link.get("state") != "active":
                continue
            if link.get("remote_type") == "gpu":
                remote = str(link.get("remote_bus_id", "")).lower()
                direct |= {(bus, remote), (remote, bus)}
            elif link.get("remote_type") == "switch":
                switched.add(bus)
    over_switch = {(a, b) for a in switched for b in switched if a != b}
    return buses, direct | over_switch


def _int_param(params: dict, key: str, index: int) -> int:
    value = params.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _bad_record(
            index,
            f"p2p_copy has no integer params.{key}",
            "a link's test names the bytes and concurrency it was measured with",
        )
    return value


def links_from_records(records: Iterable[dict], capture: Path) -> dict:
    """links.json (schema 1) from benchmark records; `capture` is the measured machine's capture
    directory. Unmeasured links are absent, never zero."""
    gpus, nvlinked = _gpus_and_nvlinks(capture)
    groups: dict[tuple[str, str, int], dict] = {}
    for index, record in enumerate(records, 1):
        if record.get("bench") != "p2p_copy":
            continue
        params = record.get("params")
        params = params if isinstance(params, dict) else {}
        # "0<->1": both directions at once, which no single link carries.
        if "<->" in str(params.get("direction", "")):
            continue
        devices = record.get("devices")
        if not isinstance(devices, dict) or not all(
            isinstance(devices.get(k), str) for k in ("src_bus", "dst_bus")
        ):
            raise _bad_record(
                index,
                "p2p_copy has no devices.src_bus and devices.dst_bus",
                "links are keyed by bus ID; CUDA ordinals are never mapped",
            )
        src, dst = devices["src_bus"].lower(), devices["dst_bus"].lower()
        if not (BUS_ID.fullmatch(src) and BUS_ID.fullmatch(dst)):
            raise _bad_record(
                index, "devices holds a malformed bus ID", "bus IDs are dddd:bb:dd.f, lowercase"
            )
        concurrency = _int_param(params, "concurrency", index)
        size = _int_param(params, "bytes", index)
        if src == dst:
            continue
        if src not in gpus or dst not in gpus:
            raise _bad_capture(
                f"record {index} measured a GPU the capture does not list",
                "pass --capture <dir> of the machine that ran the benchmarks",
            )
        samples = record.get("samples")
        if record.get("unit") != UNIT or not isinstance(samples, list) or not samples:
            raise _bad_record(
                index, f"p2p_copy has no samples in {UNIT}", "bandwidths are medians of samples"
            )
        group = groups.setdefault((src, dst, size), {"concurrency": concurrency, "samples": []})
        if group["concurrency"] != concurrency:
            raise _bad_record(
                index,
                "p2p_copy repeats a (from, to, bytes) with another concurrency",
                "links.json holds one entry per (from, to, bytes)",
            )
        group["samples"].extend(samples)
    links = []
    for (src, dst, size), group in sorted(groups.items()):
        bw_mbps = round(statistics.median(group["samples"]) * 1000)
        if bw_mbps < 1:
            raise _bad_capture(
                "a measured link rounds to 0 MB/s",
                "rerun the benchmark; a zero bandwidth is a failed measurement",
            )
        links.append(
            {
                "from": src,
                "to": dst,
                "kind": "nvlink" if (src, dst) in nvlinked else "pcie",
                "direction": "forward",
                "bw_mbps": bw_mbps,
                "test": {"bench": "p2p_copy", "bytes": size, "concurrency": group["concurrency"]},
                "samples": len(group["samples"]),
            }
        )
    return {"schema": 1, "links": links}
