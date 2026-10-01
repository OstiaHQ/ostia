#!/usr/bin/env python3
"""Reference tools for the calibration workloads (RFC-0001 §6.4).

    ostia-dev bench oracles --results results.jsonl --oracle nvbandwidth --print-command
    ostia-dev bench oracles --results results.jsonl --oracle nvbandwidth --output nvbandwidth.txt

A calibration workload passes when its median is within 5% of its reference tool, run
with matched parameters: `p2p_copy` against `nvbandwidth`; `rdma_put` against
`ib_write_bw --use_cuda` or `ucx_perftest`; the informational `tcp_put` against `iperf3`
or `ucx_perftest` over TCP. --print-command prints the matched command for the
workload's parameters (run it on the same machine); --output compares its output.
Parameters with no matched form (for example p2p_copy concurrency > 1) are refused
rather than compared against a different measurement. All rates are GB/s (1e9 B/s).
"""

import argparse
import json
import re
import shlex
import statistics
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ostia_dev.bench.schema import SchemaError, load

TOLERANCE = 0.05
MIB = 1 << 20


class OracleError(ValueError):
    pass


@dataclass(frozen=True)
class Oracle:
    name: str
    command: Callable[[dict], list[str]]
    parse: Callable[[str, dict], float]


def agree(measured: float, reference: float) -> bool:
    return abs(measured / reference - 1) <= TOLERANCE


def _no_result(tool: str) -> OracleError:
    return OracleError(f"{tool}: no result in the output; did the run fail?")


# nvbandwidth: copy-engine device-to-device copy, one copy at a time, device memory on
# both sides; the same operation as p2p_copy (cudaMemcpyPeerAsync).
def _nvbandwidth_command(p: dict) -> list[str]:
    if p.get("concurrency", 1) != 1:
        raise OracleError("nvbandwidth: no matched test for concurrency > 1")
    test = "device_to_device_memcpy_write_ce"
    if "<->" in p["direction"]:
        test = "device_to_device_bidirectional_memcpy_write_ce"
    return ["nvbandwidth", "-t", test, "-b", str(p["bytes"] // MIB), "-i", "10"]


def _nvbandwidth_parse(text: str, p: dict) -> float:
    src, dst = (int(x) for x in re.split(r"<?->", p["direction"]))
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if "bandwidth (GB/s)" not in line:
            continue
        columns = [int(c) for c in lines[i + 1].split()]
        rows = {}
        for row in lines[i + 2 :]:
            cells = row.split()
            if not cells or not cells[0].isdigit():
                break
            rows[int(cells[0])] = dict(zip(columns, cells[1:], strict=False))
        row, col = (src, dst) if "(row) ->" in line else (dst, src)
        try:
            return float(rows[row][col])
        except (KeyError, ValueError):
            raise OracleError(f"nvbandwidth: no cell for {p['direction']}") from None
    raise _no_result("nvbandwidth")


# ib_write_bw from perftest, with --use_cuda for GPU memory. Run the same command on the
# server, then on the client with the server's host name appended.
def _ib_write_bw_command(p: dict) -> list[str]:
    cmd = ["ib_write_bw", "-s", str(p["bytes"]), "-n", "100", "--report_gbits", "-F"]
    if p.get("nic"):
        cmd += ["-d", p["nic"]]
    if p.get("mem", "cuda") == "cuda":
        cmd.append(f"--use_cuda={p.get('gpu', 0)}")
    return cmd


def _ib_write_bw_parse(text: str, p: dict) -> float:
    for line in text.splitlines():
        cells = line.split()
        if len(cells) >= 4 and cells[0] == str(p["bytes"]):
            return float(cells[3]) / 8  # BW average, Gb/s
    raise _no_result("ib_write_bw")


# ucx_perftest reports bandwidth in MB/s of 2^20 bytes.
def _ucx_perftest_command(p: dict, tls: str | None = None) -> list[str]:
    cmd = ["ucx_perftest", "-t", "ucp_put_bw", "-s", str(p["bytes"]), "-n", "100"]
    if tls:
        cmd = ["env", f"UCX_TLS={tls}", *cmd]
    return cmd + ["-m", p.get("mem", "host")]


def _ucx_perftest_parse(text: str, _: dict) -> float:
    for line in text.splitlines():
        if line.startswith("Final:"):
            return float(line.split()[5]) * MIB / 1e9  # bandwidth average
    raise _no_result("ucx_perftest")


def _iperf3_command(p: dict) -> list[str]:
    return ["iperf3", "-J", "-t", "10", "-l", str(min(p["bytes"], 128 * 1024)), "-c"]


def _iperf3_parse(text: str, _: dict) -> float:
    try:
        return json.loads(text)["end"]["sum_received"]["bits_per_second"] / 8e9
    except (json.JSONDecodeError, KeyError):
        raise _no_result("iperf3") from None


ORACLES: dict[str, dict[str, Oracle]] = {
    "p2p_copy": {"nvbandwidth": Oracle("nvbandwidth", _nvbandwidth_command, _nvbandwidth_parse)},
    "rdma_put": {
        "ib_write_bw": Oracle("ib_write_bw", _ib_write_bw_command, _ib_write_bw_parse),
        "ucx_perftest": Oracle("ucx_perftest", _ucx_perftest_command, _ucx_perftest_parse),
    },
    "tcp_put": {
        "iperf3": Oracle("iperf3", _iperf3_command, _iperf3_parse),
        "ucx_perftest": Oracle(
            "ucx_perftest", lambda p: _ucx_perftest_command(p, "tcp"), _ucx_perftest_parse
        ),
    },
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ostia-dev bench oracles", description=__doc__.splitlines()[0]
    )
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--oracle", required=True)
    parser.add_argument("--bench", help="calibration workload (default: the only one in the file)")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--print-command", action="store_true")
    group.add_argument("--output", type=Path, help="the reference tool's output")
    args = parser.parse_args(argv)
    try:
        records = [r for r in load(args.results) if r["bench"] in ORACLES]
        if args.bench:
            records = [r for r in records if r["bench"] == args.bench]
        if len(records) != 1:
            raise OracleError(
                f"{args.results}: expected one calibration record, found {len(records)}; "
                "pass --bench"
            )
        record = records[0]
        oracle = ORACLES[record["bench"]].get(args.oracle)
        if oracle is None:
            known = ", ".join(ORACLES[record["bench"]])
            raise OracleError(f"{record['bench']} has no oracle '{args.oracle}' (known: {known})")
        if args.print_command:
            print(shlex.join(oracle.command(record["params"])))
            return 0
        reference = oracle.parse(args.output.read_text(), record["params"])
    except (OracleError, SchemaError, OSError) as e:
        print(f"error: {e}\n  see: RFC-0001 §6.4")
        return 1
    measured = statistics.median(record["samples"])
    verdict = "agrees with" if agree(measured, reference) else "differs from"
    print(
        f"{record['bench']} {json.dumps(record['params'], sort_keys=True)}: {measured:.2f} GB/s "
        f"{verdict} {oracle.name} {reference:.2f} GB/s ({measured / reference - 1:+.1%}, "
        f"limit ±{TOLERANCE:.0%})"
    )
    return 0 if agree(measured, reference) else 1


if __name__ == "__main__":
    sys.exit(main())
