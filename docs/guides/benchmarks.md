# Benchmarks

How to run a benchmark, compare it with a baseline, and update a baseline. The design is [RFC-0001 §6](../rfcs/0001-m0-foundations.md#6-benchmark-harness-and-the-m0-gate).

## The pieces

| Piece | What it does |
| --- | --- |
| nvbench | Times in-process GPU benchmarks: kernels and single-process copies |
| `tools/bench/ostia_bench.py` (`pixi run bench`) | Runs benchmark binaries, converts nvbench JSON, fills in provenance and compatibility fields, and writes schema-1 records to `bench/results/<run id>/results.jsonl` |
| `tools/bench/compare.py` (`pixi run compare`) | Compares a run with a baseline: `pass`, `regression`, `inconclusive`, `invalid` or `skipped` |
| `tools/bench/overhead.py` | The telemetry overhead mechanism: paired, interleaved runs of `off` against a level |
| `bench/baselines/<setup>.json` | Committed baselines, one per reference setup |

## Result records (schema 1)

One JSON object per line:
- the benchmark name and its parameters;
- the unit and whether higher is better;
- the samples, with median, p5 and p95;
- two groups of fields:
  - **provenance** (git SHA, date, run ID), which never affects comparisons;
  - **compatibility** (GPU, driver, CUDA, NIC, topology, telemetry build level, compiler, dependencies), which must be equal for two results to be compared.

`topology` is `null` until topology fixtures land (Rollout PR 6). Tools reject schema versions they do not know.

## Running a benchmark

Benchmarks need CUDA and a GPU. Build them with `OSTIA_BUILD_BENCH=ON`:

```bash
pixi run -e cuda-12 cmake --preset release -DOSTIA_BUILD_BENCH=ON
pixi run -e cuda-12 cmake --build --preset release
pixi run -e cuda-12 bench run --needs-gpu --runs 10 \
  --bench build/cuda-12/release/telemetry/bench/ostia_telemetry_bench_noop \
  --build-dir build/cuda-12/release
```

- Without a GPU, `--needs-gpu` stops with an error naming the fix. On a Mac, `pixi run check-cuda` compiles the benchmarks.
- Multi-process benchmarks run through the multi-process launcher: pass `--format ostia --ranks N`.
- Across two pods, `--remote` runs one rank: rank 0 gets `--listen` and rank 1 `--connect`, from the pod's `OSTIA_RANK`, `OSTIA_PEER_HOST` and `OSTIA_PORT` (`ostia-dev remote k8s --pods 2`, [remote-runs.md](remote-runs.md)). Only rank 1, the source, writes the record and the evidence. A second `run` with the same `--run-id` appends to its `results.jsonl`.

## Comparing with a baseline

The comparison is the relative change of the medians, signed so that positive means worse, with a 95% bootstrap interval (10,000 resamples, fixed seed):
- `pass` if the interval's upper end is below 5%;
- `regression` if its lower end is above 5%;
- `inconclusive` otherwise, or with fewer than 10 samples per side.

<!-- docs-as-test:start -->
```bash
mkdir -p bench/results/example
python3 - <<'EOF'
import json, random
rng = random.Random(1)
def record(mean, run):
    return {"schema": 1,
            "provenance": {"git_sha": "0000000", "date": "2026-10-01T00:00:00Z", "run_id": run},
            "compat": {"gpu": "none", "driver": "none", "cuda": "none", "nic": "none",
                       "topology": None, "build_level": "off", "compiler": "example",
                       "deps": "pixi.lock:example"},
            "bench": "example", "params": {"bytes": 1024}, "unit": "GB/s",
            "higher_is_better": True, "samples": [rng.gauss(mean, 0.1) for _ in range(20)]}
for name, mean in (("base", 40.0), ("same", 40.0), ("slower", 35.0)):
    with open(f"bench/results/example/{name}.jsonl", "w") as f:
        f.write(json.dumps(record(mean, name)) + "\n")
EOF
# Same numbers: pass (exit 0).
pixi run compare --baseline bench/results/example/base.jsonl --candidate bench/results/example/same.jsonl
# 12.5% less bandwidth: regression (exit 1).
if pixi run compare --baseline bench/results/example/base.jsonl --candidate bench/results/example/slower.jsonl; then
  echo "expected a regression"; exit 1
fi
```
<!-- docs-as-test:end -->

- A run with missing, duplicate, malformed or non-finite results is `invalid`. So is a missing or empty result file.
- Give `--manifest cases.json` (a list of `{"bench", "params"}`) to require every expected case.
- A required gate passes `--require-pass`, so `inconclusive` fails it.
- Results from different compatibility fields are `skipped` and listed in the summary.

## Updating a baseline

Record the setup on at least two machines if you can, then pool the runs:

```bash
pixi run compare --write-baseline bench/baselines/nvlink-node.json --setup nvlink-node \
  bench/results/<run on machine 1>/results.jsonl bench/results/<run on machine 2>/results.jsonl
```

A baseline update is **its own pull request**, and its description says why the numbers changed.

## The gate workloads

The M0 gate (RFC-0001 §6.4) runs standalone reference programs from `fabric/bench/`. They are not Ostia's data path, which arrives in M1.

| Workload | Program | Setup | Checked against |
| --- | --- | --- | --- |
| `p2p_copy` (calibration) | `ostia_fabric_bench_p2p_copy` | nvlink-node | `nvbandwidth`, within 5% |
| `pipelining` | `ostia_fabric_bench_pipelining` | nvlink-node | 90% of the slower of its pack and transfer stages |
| `batching` | `ostia_fabric_bench_batching` | nvlink-node | 90% of `m / (t0 + m / B)`, for 1 MiB and larger |
| `dual_link` | `ostia_fabric_bench_dual_link` | nvlink-node (two NVLink paths), rdma-pair (`--mode rails`, two NICs) | 90% of the sum of the two paths |
| `rdma_put` (calibration) | `ostia_fabric_bench_rdma_put` | rdma-pair | `ib_write_bw --use_cuda` or `ucx_perftest`, within 5% |
| `gdr_stream` | `ostia_fabric_bench_gdr_stream` | rdma-pair | 90% of the `rdma_put` ceiling |
| `tcp_put` (informational) | `ostia_fabric_bench_tcp_put` | tcp-efa-pair | `iperf3` or `ucx_perftest` over TCP |

- **Build and smoke tests.** The programs build with the tests: the CUDA ones in the `cuda-12` and `cuda-13` environments, and the UCX ones wherever UCX is installed.
  - Every program has `--smoke`, which runs a small size and checks checksums only.
  - CPU CI runs the UCX programs over TCP loopback: `pixi run -e ucx test-multiprocess`.
  - On one L4 node, `ostia-dev remote`'s `bench-smoke` suite ([remote-runs.md](remote-runs.md)) runs every program, with same-device copies standing in for two GPUs.
  - `--corrupt` flips one received byte, and the checksum must catch it.
- **Two-node programs.** Start rank 0 (the target) with `--listen PORT` and rank 1 (the source) with `--connect HOST:PORT`. On one machine, `fabric/tests/multiprocess/launcher.py --ranks 2` starts both.
- **Calibration.** `tools/bench/oracles.py --print-command` prints the reference tool's command, with parameters matched to the recorded result. `--output` then compares that tool's output with the result.
- **Bounds.** `tools/bench/bounds.py results.jsonl` checks each gate workload against its bound, computed from the same run.
- **Transport evidence.** `ostia_bench.py run --evidence` records what the run actually used, in `evidence/<workload>.json` next to the results:
  - the UCX lanes;
  - the registered memory type;
  - peer access;
  - per-NIC and per-NVLink traffic counters.

  `compare.py --evidence-dir` makes a case `invalid` when its evidence is missing or does not show the capability under test, for example an `rdma_put` that ran over TCP or from host memory.
- **Capability profiles.** `infra/setups/<setup>.yaml` names each setup's gate workloads and the capabilities of its primary and fallback machines (RFC-0004 §1). Before anything is rented, `tools/bench/capabilities.py` fails if a machine cannot support a gate workload. An also-run workload it cannot support is reported `unsupported`. A machine may be a k8s machine (`backend: k8s`), which `ostia-dev remote gate <setup>` runs ([remote-runs.md](remote-runs.md)); `evidence.py --probe ib|nvlink` checks first that the counters it needs are readable.

A workload counts as verified only after a real run on its target hardware, against its oracle. Those runs happen in Rollout PR 7, on the rented setups.

## The telemetry overhead mechanism

`overhead.py` alternates `off` and the level under test on the same box, at least 20 pairs:
- It computes the mean overhead `r = t_level / t_off - 1` with a 95% bootstrap interval.
- It passes below 2% and fails above 2%. Otherwise it adds pairs, up to 100, and fails if it is still undecided.
- Before the gate counts, an A/A run (`--aa`) must show a noise floor of at most ±0.5%. If an L4 node reached with `ostia-dev remote` is noisier, the gate runs on a quiet rented box (`--target rented`, with RFC-0004's tooling).
- `--self-test` must fail with a 3% slowdown injected and pass with none. Both run on a GPU node, with `ostia-dev remote`'s `overhead-aa` suite.

The acceptance gate itself turns on with the telemetry runtime (RFC-0002, Rollout PR 8).
