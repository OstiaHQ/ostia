---
number: 14
title: Replace automated GPU CI with on-demand remote test runs
status: Draft
authors: [ShAlireza]
components: [build]
created: 2026-09-29
updated: 2026-09-29
supersedes: []
superseded_by: []
discussion:
---

# ADR-0014: Replace automated GPU CI with on-demand remote test runs

## Context

[RFC-0001 §4.2](../rfcs/0001-m0-foundations.md#42-gpu-jobs) designed GPU CI for a public repository: an ephemeral AWS `g6.xlarge` (one L4) per job, started by Cirun, and fenced against untrusted fork code. The fences were label gating, a pre-checkout recheck, approval environments, default-branch-only workflows, a separate AWS account with no instance profile, and no caches crossing from PR runs to trusted runs. §4.3 gave it a $200 sub-budget of the M0 budget, enforced by an AWS Budgets action. Rollout PR 4 (#17) implements it.

Before #17 merged, the maintainer reconsidered:

- Ostia has one maintainer, who already has Kubernetes clusters with GPU nodes. Almost all of §4.2's machinery exists to run *untrusted* code automatically. On-demand runs by a maintainer, of code the maintainer chose, don't need it.
- One `g6.xlarge` type covers the L4 only. GPU tests should also run on A100 and H100 nodes when available, and some programs need two nodes (RFC-0001 §6.4).
- The maintainer works on macOS without CUDA. The loop that matters is "edit, run on a GPU, read the result", including uncommitted changes. CI runs only what is pushed.
- An interim plan, ARC runner scale sets on a spot L4 pool of the maintainer's GKE cluster, kept automated CI and so kept every fork-safety measure plus new ones. It was dropped before anything was created.

[RFC-0005](https://github.com/OstiaHQ/ostia/pull/21) designs the replacement: `ostia-dev remote k8s`, which runs Ostia's build and tests in temporary, isolated pods on any Kubernetes cluster and tears them down, and `ostia-dev remote container` for local containers.

## Decision

**Automated GPU CI is dropped. GPU testing happens in on-demand remote runs with `ostia-dev remote` (RFC-0005), started by a maintainer.** CPU CI (RFC-0001 §4.1) is unchanged.

1. **Superseded:** all of RFC-0001 §4.2 (Cirun runner, authorisation, isolation, what GPU jobs run as CI) and §4.3 (GPU CI cost and its enforcement). What GPU jobs *tested* moves to RFC-0005's suites (§3.3 there): `gpu` (every test at four telemetry levels), `sanitizer`, `bench-smoke` and `overhead-aa`.
2. **When a GPU run is required:** before merging a pull request that touches CUDA sources (`.cu`, `.cuh`), CUDA CMake, GPU tests or benchmarks, `fabric/tests/multiprocess/` or the CUDA pixi environments, a maintainer runs
   `ostia-dev remote k8s --node l4 --suite gpu --ref <head SHA>` (or on the local checkout of that head) and pastes the summary line (run ID, SHA, tree hash, result) into the pull request. The pull-request template gets a checkbox for it. Before each milestone gate, the `gpu` and `sanitizer` suites run on `main`. Contributors without a cluster ask a maintainer for the run.
3. **Budget:** runs on the maintainer's own clusters are outside the $1,500 M0 budget. The $200 GPU CI sub-budget moves to the contingency, which becomes **$500**. The rented-hardware sub-budget stays at $1,000 under RFC-0004's enforcement. Each run's summary records node-hours times the profile's declared hourly price, and `ostia-dev remote k8s usage` totals them.
4. **Support tier:** Ubuntu 24.04 x86_64 stays tier 1 (RFC-0001 §1.4). The row's backing changes from the `gpu-l4` CI job to `linux-x64-*` plus `ostia-dev remote k8s --suite gpu` on an L4 node under rule 2. The tier meaning becomes "built and tested on every PR; GPU-tested before merging GPU-affecting PRs; gate benchmarks run here".

## Consequences

**RFC-0001 "Done when" items**

| RFC-0001 item | Now |
| --- | --- |
| §1.4: `dev` GPU detection has a configure test | Unchanged on CPU (no GPU → release list). The GPU-detected half (`native`) comes from a `--suite gpu` run, which checks `ostia-summary.txt` |
| §1.4: each tier-1 row has a green CI job | Holds for the CPU jobs. The GPU part of the Ubuntu x86_64 row is backed by k8s runs under rule 2, not by a CI job |
| §4: every §4.1 job green; required checks within 20 minutes | Unchanged |
| §4: `docs-as-test` passes and records its duration | Unchanged |
| §4: a fork PR editing workflows and Cirun files can't get a GPU runner | **Superseded:** no automated GPU runner exists |
| §4: the GPU job confirms IMDS returns no IAM credentials | **Superseded.** RFC-0005 §4.6's `verify` covers the equivalent: metadata server unreachable, no token |
| §4: label-then-push cancels; the recheck catches a push; relabelling runs the new SHA | **Superseded** |
| §4: pushes to `main` and nightly runs start GPU jobs without approval | **Superseded:** replaced by rule 2's runs on `main` before each milestone gate |
| §4: the AWS Budgets action dry run | **Superseded:** no GPU CI account or sub-budget |
| §4: nightly `compute-sanitizer` survives a simulated spot interruption | **Superseded.** `--suite sanitizer` replaces the nightly job; a preempted run exits 3 and is rerun |
| §4: multi-process tests pass over UCX TCP loopback on CPU and with 2 processes on one L4 | The CPU half is unchanged. The L4 half comes from a `--suite gpu` run |
| §6: `compare.py` tests | Unchanged |
| §6: 3% injected fails, 0% passes, and an A/A run on the L4 runner reports its noise floor | Comes from `--suite overhead-aa` on an L4 node. The quiet-box fallback stays in RFC-0004 |
| §6: each gate workload exists with its oracle and records transport evidence | Unchanged |
| §6: the gate passes on nvlink-node and rdma-pair (RFC-0004) or pre-declared fallbacks | Unchanged, except that a setup may now also declare a k8s machine (RFC-0005 §6) that passes the same checks |
| §6: `docs/guides/benchmarks.md` shows how to run a benchmark and update a baseline | Unchanged. Its "GPU CI" wording changes in the #18 rework |

**Elsewhere in RFC-0001**

- §1.1's "GPU CI builds only `sm_89`" no longer applies. Remote runs build for the node's architecture (RFC-0005 §4.9).
- In the Goals, "on a GPU when a maintainer approves it" becomes rule 2. The Budget paragraph's GPU CI share moves to the contingency (rule 3).
- In Failure handling, the "GPU runner unavailable or spot-interrupted" and "Driver or CUDA mismatch on the GPU image" rows are handled by RFC-0005's failure table (exit 3, fingerprint printed).
- The Testing table's "GPU CI" rows become k8s runs. The four security and budget rows are superseded with §4.2.
- The Cirun items in Alternatives considered and Open questions are moot.
- Rollout PR 4 becomes the #17 rework below.

This ADR's pull request adds a pointer to this ADR at the top of §4.2 and §4.3, strikes through the superseded "Done when" items (the text stays readable), and updates the §1.4 tier row. It also updates the PRD's D9 wording and RFC-0004's notes on GPU CI.

**Follow-up work, after this ADR is accepted**

- **#17** keeps the GPU tests the runner will run: `telemetry/tests/gpu_smoke_test.cu`, the CUDA variant of `fabric/tests/multiprocess/ucx_put.cpp` with the launcher's `--tls`/`--expect`, `tools/ci/gpu_preflight.sh` and `cuda-sanitizer-api`. It drops the GPU workflows, `infra/gpu-ci/`, `gpu_recheck.py`, `classify_failure.py`, `check_imds.sh` and their tests, `docs/guides/gpu-ci.md`, and the GPU CI text in `MAINTAINERS.md`, `CONTRIBUTING.md`, `docs/guides/README.md` and `docs/guides/building.md` (including the tier table). It is retitled as a test change.
- **#18** removes its two GPU workflow steps and rewords "GPU CI" in `docs/guides/benchmarks.md`, `tools/bench/overhead.py` and `tools/bench/ostia_bench.py` as "on a GPU node, with `ostia-dev remote`".
- **#19** rewords "GPU CI runs every program on one L4" in `docs/guides/benchmarks.md` and `fabric/bench/CMakeLists.txt`. Its setup files later gain the `backend: k8s` machine type (RFC-0005 §6).
- The pull-request template gets rule 2's checkbox.

**What becomes easier:** no CI account, Cirun app, approval environments or budget action to maintain. GPU runs on any GPU type the maintainer's clusters have, and on uncommitted work.

**What becomes harder:** GPU coverage depends on a maintainer remembering rule 2. Nothing blocks a merge automatically. Contributors can't trigger GPU runs themselves. Cluster spend is tracked per run but not enforced.
