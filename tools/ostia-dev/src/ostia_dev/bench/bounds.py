#!/usr/bin/env python3
"""Bounds of the M0 gate workloads (RFC-0001 §6.4).

    ostia-dev bench bounds results.jsonl [--ceiling GB/s]

Each gate workload other than the calibrations must reach at least 90% of its bound,
computed from measurements of the same run:
- `pipelining`: the slower of the measured pack and transfer stage rates;
- `batching`: m / (t0 + m / B) for message size m, where t0 is the measured time per
  message at the smallest size and B is the ceiling (the run's `p2p_copy`, or --ceiling);
  only sizes of 1 MiB and larger are checked;
- `dual_link`: the sum of the two paths' ceilings, or the measured limit of a shared
  resource when the run has one and it is lower;
- `gdr_stream`: the `rdma_put` ceiling measured on the same pair.
All rates are GB/s (1e9 bytes per second), and a measurement is its median.
"""

import argparse
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ostia_dev.bench.schema import SchemaError, load

TARGET = 0.90
MIB = 1 << 20


class BoundsError(ValueError):
    pass


@dataclass
class Check:
    bench: str
    params: dict
    measured: float
    bound: float

    @property
    def ratio(self) -> float:
        return self.measured / self.bound

    @property
    def ok(self) -> bool:
        return self.ratio >= TARGET


def batching_bound(m: int, t0: float, ceiling: float) -> float:
    """GB/s for m-byte messages with a fixed per-message cost t0 (s) and ceiling (GB/s)."""
    return m / (t0 + m / (ceiling * 1e9)) / 1e9


def _median(r: dict) -> float:
    return statistics.median(r["samples"])


def _one(records: list[dict], what: str, **match) -> float:
    found = [r for r in records if all(r["params"].get(k) == v for k, v in match.items())]
    if not found:
        wanted = ", ".join(f"{k}={v}" for k, v in match.items())
        raise BoundsError(f"{what}: the run has no record with {wanted}")
    return _median(found[0])


def _pipelining(rs: list[dict], _: dict) -> list[Check]:
    out = []
    for r in (r for r in rs if r["params"].get("mode") == "pipelined"):
        n = r["params"]["bytes"]
        pack = _one(rs, "pipelining (pack stage)", mode="pack", bytes=n)
        transfer = _one(rs, "pipelining (transfer stage)", mode="transfer", bytes=n)
        out.append(Check("pipelining", r["params"], _median(r), min(pack, transfer)))
    return out


def _batching(rs: list[dict], ctx: dict) -> list[Check]:
    ceiling = ctx["ceiling"]
    if ceiling is None:
        raise BoundsError("batching: no ceiling; run p2p_copy in the same run or pass --ceiling")
    smallest = min(rs, key=lambda r: r["params"]["bytes"])
    m0 = smallest["params"]["bytes"]
    t0 = m0 / (_median(smallest) * 1e9)
    return [
        Check(
            "batching", r["params"], _median(r), batching_bound(r["params"]["bytes"], t0, ceiling)
        )
        for r in sorted(rs, key=lambda r: r["params"]["bytes"])
        if r["params"]["bytes"] >= MIB
    ]


def _dual_link(rs: list[dict], _: dict) -> list[Check]:
    out = []
    for r in (r for r in rs if r["params"].get("path") == "both"):
        n = r["params"]["bytes"]
        bound = _one(rs, "dual_link (path a)", path="a", bytes=n) + _one(
            rs, "dual_link (path b)", path="b", bytes=n
        )
        shared = [
            _median(s)
            for s in rs
            if s["params"].get("path") == "shared_limit" and s["params"]["bytes"] == n
        ]
        out.append(Check("dual_link", r["params"], _median(r), min([bound, *shared])))
    return out


def _gdr_stream(rs: list[dict], ctx: dict) -> list[Check]:
    if ctx["rdma_put"] is None:
        raise BoundsError("gdr_stream: no rdma_put ceiling; run rdma_put on the same pair")
    return [Check("gdr_stream", r["params"], _median(r), ctx["rdma_put"]) for r in rs]


BOUNDS = {
    "pipelining": _pipelining,
    "batching": _batching,
    "dual_link": _dual_link,
    "gdr_stream": _gdr_stream,
}


def _largest(records: list[dict], bench: str) -> float | None:
    rs = [r for r in records if r["bench"] == bench]
    return _median(max(rs, key=lambda r: r["params"].get("bytes", 0))) if rs else None


def check(records: list[dict], ceiling: float | None = None) -> list[Check]:
    ctx = {
        "ceiling": ceiling if ceiling is not None else _largest(records, "p2p_copy"),
        "rdma_put": _largest(records, "rdma_put"),
    }
    out: list[Check] = []
    for bench, fn in BOUNDS.items():
        rs = [r for r in records if r["bench"] == bench]
        if rs:
            out += fn(rs, ctx)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ostia-dev bench bounds", description=__doc__.splitlines()[0]
    )
    parser.add_argument("results", type=Path)
    parser.add_argument("--ceiling", type=float, help="batching ceiling B in GB/s")
    args = parser.parse_args(argv)
    try:
        checks = check(load(args.results), args.ceiling)
    except (BoundsError, SchemaError, OSError) as e:
        print(f"error: {e}\n  see: RFC-0001 §6.4")
        return 1
    print("| Workload | Params | Measured GB/s | Bound GB/s | Ratio | Result |")
    print("| --- | --- | --- | --- | --- | --- |")
    for c in checks:
        result = "ok" if c.ok else f"below {TARGET:.0%}"
        cells = [c.bench, c.params, f"{c.measured:.2f}", f"{c.bound:.2f}", f"{c.ratio:.1%}", result]
        print("| " + " | ".join(map(str, cells)) + " |")
    return 0 if all(c.ok for c in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
