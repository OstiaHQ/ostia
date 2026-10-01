#!/usr/bin/env python3
"""The Ostia benchmark driver (RFC-0001 §6.1).

    ostia_bench.py run --bench <binary> [--format nvbench|ostia] [--runs 10] [--ranks N | --remote]
                       [--tls UCX_TLS] [--evidence] [--needs-gpu]
                       [--build-dir build/cuda-12/release] [--run-id ID] [-- <program args>]
    ostia_bench.py convert --run-id ID <nvbench.json>...
    ostia_bench.py median-seconds --bench <nvbench binary>

`run` executes a benchmark binary --runs times, in-process ones (nvbench) directly and
multi-process ones through fabric/tests/multiprocess/launcher.py (--ranks), and writes
schema-1 records (tools/bench/schema.py) to bench/results/<run id>/results.jsonl. One
nvbench invocation gives one sample per state: its cold mean GPU time, or its global
memory bandwidth when nvbench reports one. `ostia` format binaries print schema-1
records without provenance and compat, which the driver fills in. `--evidence` records
the transport the run used (tools/bench/evidence.py) in
bench/results/<run id>/evidence/<workload>.json, which gate comparisons require (§6.4).
`--remote` runs one rank of a two-pod run (RFC-0005 §4.11): rank 0 gets --listen and
rank 1 --connect, from the pod's OSTIA_RANK, OSTIA_SIZE, OSTIA_PEER_HOST and OSTIA_PORT.
`median-seconds` prints one duration for tools/bench/overhead.py.
"""

import argparse
import datetime
import hashlib
import json
import os
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.bench import evidence
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


class BenchError(Exception):
    pass


class BenchUsage(BenchError):
    pass


DNS_RETRY, DNS_EVERY = 120, 2


def _remote_usage(problem: str) -> BenchUsage:
    return BenchUsage(
        f"error: {problem}\n"
        "  rule: --remote runs one rank of a two-pod run, with OSTIA_RANK 0 or 1, "
        "OSTIA_SIZE=2, OSTIA_PEER_HOST and OSTIA_PORT set by ostia-dev\n"
        "  fix: run it inside ostia-dev remote k8s --pods 2, or use --ranks on one machine\n"
        "  see: RFC-0005 §4.11"
    )


def _remote_args(env) -> tuple[int, list[str]]:
    """The rendezvous arguments for this pod's rank."""
    rank, size = env.get("OSTIA_RANK"), env.get("OSTIA_SIZE")
    host, port = env.get("OSTIA_PEER_HOST"), env.get("OSTIA_PORT")
    if rank not in ("0", "1"):
        raise _remote_usage(f"OSTIA_RANK is {rank!r}, not 0 or 1")
    if size != "2":
        raise _remote_usage(f"OSTIA_SIZE is {size!r}, not 2")
    for name, value in (("OSTIA_PEER_HOST", host), ("OSTIA_PORT", port)):
        if not value:
            raise _remote_usage(f"{name} is not set")
    if rank == "0":
        return 0, ["--listen", port]
    # The headless Service's record appears only once pod 0 has an address. Only the name
    # is retried here: a test connection would take rank 0's single accept.
    deadline = time.monotonic() + DNS_RETRY
    while True:
        try:
            socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
            return 1, ["--connect", f"{host}:{port}"]
        except socket.gaierror as e:
            if time.monotonic() >= deadline:
                raise BenchError(
                    f"error: rank 1 cannot resolve its peer {host} after {DNS_RETRY}s ({e})\n"
                    "  rule: rank 0's pod must be running for its Service record to exist\n"
                    "  fix: check rank 0's log in the run's results\n"
                    "  see: RFC-0005 §4.11"
                ) from e
            time.sleep(DNS_EVERY)


def unmeasured(doc: dict) -> list[str]:
    """States that ran but have no measurement: nvbench exits 0 when it discarded every
    sample, for example as throttled on a GPU another job holds at its power cap."""
    return [
        f"{bench['name']} [{state['name']}]"
        for bench in doc["benchmarks"]
        for state in bench["states"]
        if not state.get("is_skipped")
        and not {TIME, BANDWIDTH} & {s["tag"] for s in state["summaries"]}
    ]


def _measured(doc: dict, log: str = "") -> dict:
    missing = unmeasured(doc)
    if missing:
        warns = [line.strip() for line in log.splitlines() if "Warn:" in line]
        # the first warning names the cause; the last ones say how the measurement ended
        why = warns[:1] + [w for w in warns[-3:] if w not in warns[:1]]
        raise BenchError(
            f"error: nvbench measured nothing for {', '.join(missing)}\n"
            + "".join(f"  nvbench: {w}\n" for w in why)
            + "  rule: every state that runs must give a measurement; a dropped sample "
            "would skew the record\n"
            "  fix: run on a GPU no other job shares (a whole node with ostia-dev remote, "
            "or a rented box, RFC-0004)\n"
            "  see: RFC-0001 §6.1"
        )
    return from_nvbench(doc)


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def provenance_and_compat(run_id: str, build_dir: Path | None, nic: str = "none") -> tuple:
    # A remote pod has no .git; ostia-dev passes the commit it uploaded (RFC-0005 §3.4).
    sha = (
        os.environ.get("OSTIA_GIT_SHA")
        or _run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"])
        or "unknown"
    )
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


def _collect(measurements: list[dict]) -> dict:
    samples: dict = {}
    for measured in measurements:
        for key, (unit, higher, value) in measured.items():
            samples.setdefault(key, (unit, higher, []))[2].append(value)
    return samples


def _write(records: list[dict], out: Path, run_id: str) -> Path:
    for r in records:
        validate(r)
    path = out / run_id / "results.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    # one run ID may hold several programs' records, as a gate run does (RFC-0005 §6)
    with path.open("a") as f:
        f.write("".join(json.dumps(r) + "\n" for r in records))
    print(f"wrote {len(records)} records to {path}")
    return path


def _write_evidence(output: str, before: dict, after: dict, run_dir: Path) -> None:
    benches = {json.loads(line)["bench"] for line in output.splitlines() if line.startswith("{")}
    for bench in sorted(benches):
        path = run_dir / "evidence" / f"{bench}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(evidence.build(bench, before, after, output), indent=2) + "\n")
        print(f"wrote transport evidence to {path}")


def _nvbench_once(binary: str) -> dict:
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "nvbench.json"
        proc = subprocess.run([binary, "--json", str(out)], capture_output=True, text=True)
        log = proc.stdout + proc.stderr
        if proc.returncode != 0:
            tail = "".join(f"  {line}\n" for line in log.strip().splitlines()[-20:])
            raise BenchError(
                f"error: {binary} exited {proc.returncode}\n{tail}  see: RFC-0001 §6.1"
            )
        return _measured(json.loads(out.read_text()), log)


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
    run.add_argument("--remote", action="store_true", help="one rank of a two-pod run")
    run.add_argument("--needs-gpu", action="store_true")
    run.add_argument("--build-dir", type=Path)
    run.add_argument("--nic", default="none")
    run.add_argument("--tls", help="UCX_TLS for multi-process runs (default: the launcher's)")
    run.add_argument("--evidence", action="store_true", help="record transport evidence")
    conv = sub.add_parser("convert")
    conv.add_argument("files", nargs="+", type=Path)
    for p in (run, conv):
        p.add_argument("--run-id", default=None)
        p.add_argument("--out", type=Path, default=ROOT / "bench" / "results")
    med = sub.add_parser("median-seconds")
    med.add_argument("--bench", required=True)
    argv = list(sys.argv[1:] if argv is None else argv)
    program_args = argv[argv.index("--") + 1 :] if "--" in argv else []
    args = parser.parse_args(argv[: argv.index("--")] if "--" in argv else argv)

    try:
        return _main(args, program_args)
    except BenchUsage as e:
        print(e, file=sys.stderr)
        return 2
    except BenchError as e:
        print(e, file=sys.stderr)
        return 1


def _main(args: argparse.Namespace, program_args: list[str]) -> int:
    if args.command == "median-seconds":
        values = [v for _, _, v in _nvbench_once(args.bench).values()]
        print(statistics.median(values))
        return 0
    run_id = (
        args.run_id
        or datetime.datetime.now(datetime.UTC).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    )
    if args.command == "convert":
        prov, compat = provenance_and_compat(run_id, None)
        docs = [_measured(json.loads(f.read_text())) for f in args.files]
        _write(_records(_collect(docs), prov, compat), args.out, run_id)
        return 0
    if args.remote and (args.ranks or args.format != "ostia"):
        raise _remote_usage("--remote needs --format ostia and cannot take --ranks")
    if args.needs_gpu and not shutil.which("nvidia-smi"):
        return _needs_gpu_error()
    prov, compat = provenance_and_compat(run_id, args.build_dir, args.nic)
    if args.format == "nvbench":
        docs = [_nvbench_once(args.bench) for _ in range(args.runs)]
        records = _records(_collect(docs), prov, compat)
    else:
        rank, rendezvous = _remote_args(os.environ) if args.remote else (None, [])
        cmd = [args.bench, *program_args, *rendezvous]
        if args.ranks:
            launcher = ROOT / "fabric" / "tests" / "multiprocess" / "launcher.py"
            tls = ["--tls", args.tls, "--expect", ""] if args.tls else []
            cmd = [sys.executable, str(launcher), "--ranks", str(args.ranks), *tls, "--", *cmd]
        env = dict(os.environ, OSTIA_BENCH_RUNS=str(args.runs))
        before = evidence.snapshot() if args.evidence and rank != 0 else None
        out = subprocess.run(cmd, check=True, capture_output=True, text=True, env=env).stdout
        if rank == 0:
            return 0  # the target prints no record; the source, rank 1, does (RFC-0005 §4.11)
        if before is not None:
            _write_evidence(out, before, evidence.snapshot(), args.out / run_id)
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
