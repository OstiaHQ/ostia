#!/usr/bin/env python3
"""The Ostia benchmark driver (RFC-0001 §6.1).

    ostia_bench.py run --bench <binary> [--format nvbench|ostia] [--runs 10] [--ranks N]
                       [--needs-gpu] [--build-dir build/cuda-12/release] [--run-id ID]
    ostia_bench.py convert --run-id ID <nvbench.json>...
    ostia_bench.py median-seconds --bench <nvbench binary>

`run` executes a benchmark binary --runs times, in-process ones (nvbench) directly and
multi-process ones through fabric/tests/multiprocess/launcher.py (--ranks), and writes
schema-1 records (tools/bench/schema.py) to bench/results/<run id>/results.jsonl. One
nvbench invocation gives one sample per state: its cold mean GPU time, or its global
memory bandwidth when nvbench reports one. `ostia` format binaries print schema-1
records without provenance and compat, which the driver fills in. `median-seconds`
prints one duration for tools/bench/overhead.py.
"""

import argparse
import datetime
import hashlib
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.bench.schema import validate

ROOT = Path(__file__).resolve().parents[2]
TIME = "nv/cold/time/gpu/mean"
BANDWIDTH = "nv/cold/bw/global/bytes_per_second"


def _value(summary: dict) -> float:
    return float(next(d["value"] for d in summary["data"] if d["name"] == "value"))


def from_nvbench(doc: dict) -> dict[tuple[str, str], tuple[str, bool, float]]:
    """(bench, params) -> (unit, higher_is_better, value) for every measured state."""
    out = {}
    for bench in doc["benchmarks"]:
        for state in bench["states"]:
            if state.get("is_skipped"):
                continue
            params = {
                a["name"]: int(a["value"]) if a["type"] == "int64" else a["value"]
                for a in state["axis_values"]
            }
            summaries = {s["tag"]: s for s in state["summaries"]}
            key = (bench["name"], json.dumps(params, sort_keys=True))
            if BANDWIDTH in summaries:
                out[key] = ("GB/s", True, _value(summaries[BANDWIDTH]) / 1e9)
            elif TIME in summaries:
                out[key] = ("s", False, _value(summaries[TIME]))
    return out


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def provenance_and_compat(run_id: str, build_dir: Path | None, nic: str = "none") -> tuple:
    sha = _run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"]) or "unknown"
    date = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    gpu = _run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"]).splitlines()
    driver = _run(["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"])
    summary = {}
    if build_dir and (build_dir / "ostia-summary.txt").exists():
        for line in (build_dir / "ostia-summary.txt").read_text().splitlines():
            k, _, v = line.partition(": ")
            summary[k] = v
    lock = ROOT / "pixi.lock"
    deps = hashlib.sha256(lock.read_bytes()).hexdigest()[:12] if lock.exists() else "none"
    compat = {
        "gpu": gpu[0] if gpu else "none",
        "driver": driver.splitlines()[0] if driver else "none",
        "cuda": summary.get("cuda_toolkit", "none").split(" ")[0],
        "nic": nic,
        "topology": None,  # RFC-0003's topo1 identity arrives with fixtures (Rollout PR 6)
        "build_level": summary.get("telemetry_level", "unknown").split(" ")[0],
        "compiler": summary.get("compiler", "unknown").split(" (")[0],
        "deps": f"pixi.lock:{deps}",
    }
    return {"git_sha": sha, "date": date, "run_id": run_id}, compat


def _records(samples: dict, prov: dict, compat: dict) -> list[dict]:
    records = []
    for (bench, params), (unit, higher, values) in sorted(samples.items()):
        s = sorted(values)
        records.append(
            {
                "schema": 1,
                "provenance": prov,
                "compat": compat,
                "bench": bench,
                "params": json.loads(params),
                "unit": unit,
                "higher_is_better": higher,
                "samples": values,
                "median": statistics.median(s),
                "p5": s[int(0.05 * (len(s) - 1))],
                "p95": s[int(0.95 * (len(s) - 1))],
            }
        )
    return records


def _collect(docs: list[dict]) -> dict:
    samples: dict = {}
    for doc in docs:
        for key, (unit, higher, value) in from_nvbench(doc).items():
            samples.setdefault(key, (unit, higher, []))[2].append(value)
    return samples


def _write(records: list[dict], out: Path, run_id: str) -> Path:
    for r in records:
        validate(r)
    path = out / run_id / "results.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    print(f"wrote {len(records)} records to {path}")
    return path


def _nvbench_once(binary: str) -> dict:
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "nvbench.json"
        subprocess.run([binary, "--json", str(out)], check=True, capture_output=True)
        return json.loads(out.read_text())


def _needs_gpu_error() -> int:
    print(
        "error: this benchmark needs a CUDA GPU, and nvidia-smi found none\n"
        "  rule: GPU benchmarks run on a GPU node, with ostia-dev remote (RFC-0005)\n"
        "  fix: run it there or on a rented setup (RFC-0004), or compile only with "
        "pixi run check-cuda\n"
        "  see: RFC-0001 §6.1",
        file=sys.stderr,
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--bench", required=True)
    run.add_argument("--format", choices=["nvbench", "ostia"], default="nvbench")
    run.add_argument("--runs", type=int, default=10)
    run.add_argument("--ranks", type=int, default=0, help="multi-process: ranks for launcher.py")
    run.add_argument("--needs-gpu", action="store_true")
    run.add_argument("--build-dir", type=Path)
    run.add_argument("--nic", default="none")
    conv = sub.add_parser("convert")
    conv.add_argument("files", nargs="+", type=Path)
    for p in (run, conv):
        p.add_argument("--run-id", default=None)
        p.add_argument("--out", type=Path, default=ROOT / "bench" / "results")
    med = sub.add_parser("median-seconds")
    med.add_argument("--bench", required=True)
    args = parser.parse_args(argv)

    if args.command == "median-seconds":
        values = [v for _, _, v in from_nvbench(_nvbench_once(args.bench)).values()]
        print(statistics.median(values))
        return 0
    run_id = (
        args.run_id
        or datetime.datetime.now(datetime.UTC).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    )
    if args.command == "convert":
        prov, compat = provenance_and_compat(run_id, None)
        docs = [json.loads(f.read_text()) for f in args.files]
        _write(_records(_collect(docs), prov, compat), args.out, run_id)
        return 0
    if args.needs_gpu and not shutil.which("nvidia-smi"):
        return _needs_gpu_error()
    prov, compat = provenance_and_compat(run_id, args.build_dir, args.nic)
    if args.format == "nvbench":
        docs = [_nvbench_once(args.bench) for _ in range(args.runs)]
        records = _records(_collect(docs), prov, compat)
    else:
        cmd = [args.bench]
        if args.ranks:
            launcher = ROOT / "fabric" / "tests" / "multiprocess" / "launcher.py"
            cmd = [sys.executable, str(launcher), "--ranks", str(args.ranks), "--", args.bench]
        env = dict(os.environ, OSTIA_BENCH_RUNS=str(args.runs))
        out = subprocess.run(cmd, check=True, capture_output=True, text=True, env=env).stdout
        records = []
        for line in out.splitlines():
            if line.startswith("{"):
                r = json.loads(line)
                r.update(schema=1, provenance=prov, compat=compat)
                records.append(r)
    _write(records, args.out, run_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
