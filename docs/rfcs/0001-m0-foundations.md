---
number: 1
title: M0 foundations
status: Draft
authors: [ShAlireza]
components: [build, telemetry, fabric, docs]
created: 2026-09-25
updated: 2026-09-25
supersedes: []
superseded_by: []
discussion: https://github.com/OstiaHQ/ostia/pull/7
---

# RFC-0001: M0 foundations

## Summary

This RFC designs the foundations that M0 builds before any Ostia data path exists: the toolchain and supported platforms, how dependencies are obtained, the repository and CMake layout with enforced layering, CI (including GPU CI on a public repository), the telemetry build levels, the benchmark harness and the M0 gate, and how the competitive landscape is verified. Two parts are designed in their own RFCs and summarised here: topology fixtures ([RFC-0003, PR #8](https://github.com/OstiaHQ/ostia/pull/8)) and rented-hardware automation ([RFC-0004, PR #9](https://github.com/OstiaHQ/ostia/pull/9)). The telemetry runtime (counters, trace rings, exporters) is RFC-0002, which is reserved and written next.

When this RFC is implemented, every later pull request builds and tests on Linux and macOS, runs on a GPU when a maintainer approves it, and can cite a numbered, checkable requirement from this document.

## Motivation

The [PRD](../product/prd.md#roadmap-and-milestones) defines M0 as the new repository, CMake and CI, a benchmark harness, a telemetry framework, a decision log, a verified competitive landscape, and cheap testing without clusters. Its gate is that the harness reproduces the prototype's baselines: peer-to-peer NVLink bandwidth and GPUDirect RDMA near line rate.

Four founding decisions shape the design:

- **D7:** Layer 1 is C++20 + CUDA with a stable C ABI and Python bindings (nanobind), sanitizers in CI.
- **D9:** there is no owned cluster. Everyday CI uses CPU runners plus one rented single-GPU runner; gate benchmarks run on rented reference setups brought up and torn down by one command.
- **D10:** observability is built in from M0, with build levels `off`, `metrics`, `trace` and `debug`.
- **D12:** components are `ostia-fabric`, `ostia-exchange`, `ostia-runtime`, `ostia-query` and `ostia-telemetry` in one public monorepo, with layering enforced by the build.

The repository is public and the project has one maintainer, so three constraints run through every section: contributors without GPUs must be able to do real work, untrusted pull requests must never reach cloud credentials, and money spent on rented hardware must be bounded.

## Goals and non-goals

**Goals**

- A contributor goes from clone to passing tests on macOS or Linux in three commands, without a GPU or a cloud account.
- Every pull request builds on Linux x86_64, Linux aarch64 and macOS, and on a GPU when a maintainer approves it.
- Layering between components is enforced by configure-time checks and CI, not by convention.
- The benchmark harness produces results that can be compared across commits and that refuse to pass when the evidence is not good enough.
- The M0 gate is measured on rented reference setups, with calibration against independent tools.

**M0 is done when** every "Done when" list in §1–§9 is met, RFC-0002 (telemetry runtime), RFC-0003 (topology fixtures) and RFC-0004 (rented hardware) are accepted and implemented, and the gate runs in §6 pass.

**Budget.** M0 spends at most **$1,500** on rented hardware and GPU CI together, split into enforced sub-budgets (see [§4](#4-ci) and RFC-0004). There is a **checkpoint 10 weeks after this RFC is accepted**. Reaching the budget or the checkpoint suspends new launches and triggers a re-scope review, recorded as an ADR.

**Non-goals**

- The public Fabric and Exchange APIs, the C ABI surface and the wire protocol. They belong to the fabric and exchange RFCs.
- The telemetry runtime: runtime switch, sampling, counters, rings, OpenTelemetry, Perfetto and NVTX export. RFC-0002.
- Packaging and wheels. `manylinux_2_28` is recorded as the future wheel target.
- ABI compatibility checks before the first tagged release.
- Kernels that need architecture-specific targets (`sm_90a`, `sm_100a`).

## Design

### 1. Toolchain

#### 1.1 CUDA and GPU architectures

- **Minimum CUDA is 12.8.** It is the first 12.x release with Blackwell (`sm_100`) support and runs on the older drivers that clusters keep. CI also builds with the latest CUDA 13.x (13.4 at the time of writing). CUDA 13 needs driver R580 or newer. The floor is revisited by ADR.
- **Architectures:** SASS for `sm_80`, `sm_90` and `sm_100`, plus PTX for the newest listed architecture. `sm_80` SASS runs on `sm_86` and `sm_89`, so L4 and A10G CI machines need no extra target. Architectures not in the list, such as `sm_103` and `sm_120`, run through PTX JIT; set `CUDA_CACHE_PATH` so the JIT cost is paid once. Architecture-specific targets (`sm_90a`, `sm_100a`, needed for TMA and wgmma) are left to the kernel RFCs that need them.
- A user-set `CMAKE_CUDA_ARCHITECTURES` always wins. The `dev` preset uses `native` only when a GPU is detected, because CMake fails at configure time when `native` is set and no GPU is present; otherwise it uses the release list. The configure summary prints the choice.

#### 1.2 Host compilers and language

- nvcc compiles device code. Clang as the CUDA compiler is not supported in v1.
- Supported host compilers, bounded by what each CUDA version accepts:

| CUDA | GCC | Clang |
| --- | --- | --- |
| 12.8 | 11–14 | 17–19 |
| 13.x | 11–16 | 17–22 |

- The nvcc compile-only CI jobs use GCC 11 with CUDA 12.8 and GCC 14 with CUDA 13.x.
- **C++20 everywhere** (D7). Public headers must compile as C++20 inside `.cu` translation units. The usable standard library is what GCC 11 and nvcc 12.8 support:
  - allowed: concepts, `<span>`, `<ranges>`, `<bit>` and `std::bit_cast`, `<source_location>`, `std::jthread`, `<latch>`, `<barrier>`, `<semaphore>`;
  - banned: `<format>` (GCC 13), constexpr `std::string`/`std::vector` and `std::atomic<std::shared_ptr>` (GCC 12), chrono time zones (GCC 14).
  The GCC 11 job enforces the list by building everything.
- There is no `std::expected` in C++20, so Ostia has its own `ostia::Result<T>` whose member names mirror `std::expected`, making a later switch mechanical.
- **C++23** is revisited by ADR when the floors reach CUDA 13.3 (the first nvcc with C++23) and GCC 13.

#### 1.3 CMake

CMake **4.1 or newer**. It is the first line that knows both CUDA 13 and the 12.8 architectures, and pixi supplies it, so a high floor costs contributors nothing.

#### 1.4 Platforms and support tiers

| Tier | Meaning | Platforms | CI job that backs it |
| --- | --- | --- | --- |
| 1 | Built and tested on every PR; gate benchmarks run here | Ubuntu 24.04 x86_64 | `linux-x64-*`, `gpu-l4` |
| 1 | Built and tested on every PR | macOS 15 arm64, host-only (no CUDA) | `macos-arm64-host` |
| 2 | Built on every PR, tested where possible | Ubuntu 24.04 aarch64; Ubuntu 22.04 and Rocky 9 x86_64 (distro packages, no pixi) | `linux-arm64-*`, `container-ubuntu2204`, `container-rocky9` |
| 3 | Best effort | Other Linux distributions; building without pixi elsewhere | none |

aarch64 moves to tier 1 after a gate run passes on a rented Grace Hopper machine. On macOS, CUDA code is checked with a `linux/arm64` container that has the CUDA SBSA toolkit; it runs natively on Apple silicon and nvcc needs no GPU.

**Done when**

- [ ] `CMakePresets.json` encodes the architecture list, the `dev` fallback and the compiler ranges above.
- [ ] The GCC 11 job builds all code with CUDA 12.8, and the GCC 14 job builds it with CUDA 13.x.
- [ ] `ostia::Result<T>` exists with `value()`, `error()`, `has_value()` and `operator bool` named as in `std::expected`.
- [ ] The support-tier table is in `docs/guides/building.md`, and each tier-1 row has a green CI job.

### 2. Dependencies

#### 2.1 How the build finds dependencies

CMake uses `find_package()` only. It never downloads, builds or installs system packages itself, so anyone embedding Ostia can supply dependencies from conda, Spack, distro packages or wheels. What each source provides:

| Source | Provides | Who uses it |
| --- | --- | --- |
| pixi (conda-forge) | CUDA toolkit, UCX + rdma-core (Linux), libhwloc, libopentelemetry-cpp (+ protobuf, curl), CMake, Ninja, ccache, clang tools, ruff, gersemi, pytest, scikit-build-core | Contributors and CI |
| CPM.cmake, pinned by tag and commit SHA | nanobind, GoogleTest, nvbench, nanoarrow, CCCL | The source build; `CPM_USE_LOCAL_PACKAGES` and `CPM_<pkg>_SOURCE` let packagers substitute their own |
| Loaded at runtime with `dlopen` | NVML, nvCOMP | Never linked or bundled |
| The consumer | Everything above that `find_package()` looks for | People embedding Ostia |

#### 2.2 pixi environments

pixi uses per-platform features, because conda-forge builds UCX and rdma-core only for Linux:

- **`default`** (host-only): no CUDA toolkit, no UCX, no rdma-core, no cloud credentials. It resolves and builds on macOS arm64, Linux x86_64 and Linux aarch64.
- **`cuda-12`** and **`cuda-13`** (Linux only): add the CUDA toolkit and CUDA-enabled UCX builds.

`pixi.lock` covers `linux-64`, `linux-aarch64` and `osx-arm64`. `docs/guides/building.md` states each environment's download size and hardware needs.

#### 2.3 Approved M0 dependency set

This RFC is the approval that docs/README.md requires for new dependencies.

| Dependency | Licence | Purpose | Source | Integration milestone | Required by |
| --- | --- | --- | --- | --- | --- |
| hwloc | BSD-3-Clause | Topology discovery | pixi | M0 (fixture replay), M1 (live) | fabric |
| UCX + rdma-core | BSD-3-Clause, BSD/GPL-2.0 dual | Transports, multi-process tests | pixi, Linux | M0 (tests), M1 | fabric tests |
| opentelemetry-cpp | Apache-2.0 | Metrics and span export | pixi | With RFC-0002 | telemetry, `metrics`/`trace` builds only |
| CCCL | Apache-2.0 with LLVM exception | CUB, Thrust, libcu++ | CPM, from GitHub | M0 | CUDA targets |
| nanobind | BSD-3-Clause | Python bindings | CPM, one version for all extensions | M0 | every `python/` |
| GoogleTest | BSD-3-Clause | C++ tests | CPM | M0 | tests |
| nvbench | Apache-2.0 | In-process GPU benchmarks | CPM (not on conda-forge) | M0 | `bench/` |
| nanoarrow | Apache-2.0 | Arrow C Device Data Interface (`NANOARROW_DEVICE_WITH_CUDA`) | CPM | M2 | exchange |
| NVML | NVIDIA driver | GPU and NVLink discovery | `dlopen` | M0 (capture), M1 | fabric |
| nvCOMP | NVIDIA proprietary, optional | Compression pushdown | `dlopen`, never bundled | M2 | exchange; out of default M0 resolution |

- CCCL is taken from GitHub rather than the toolkit, which CCCL supports ("a newer CCCL with an older CUDA Toolkit"), so the CCCL version does not change with the CUDA version. CCCL 3.x supports only the latest patch release of CUDA 12.x and 13.x.
- nvCOMP is approved now and integrated with compression pushdown in M2. When integrated, it reports its capability explicitly, and any benchmark or test that requires it fails, rather than silently falling back, when it is missing or incompatible. Its redistribution terms are checked before any binary release (PRD, Third-party code).

#### 2.4 Pinning and updates

- `pixi.lock` pins conda packages. CPM pins by tag and full commit SHA, and CI caches `CPM_SOURCE_CACHE`. When an upstream tag disappears, the fix is a pull request that bumps the pin.
- **Renovate** updates pixi (its `pixi` manager, with `pixi` in `allowedUnsafeExecutions` so it can regenerate `pixi.lock`) and CPM pins (a regex manager over `CPMAddPackage`, using github-tags). Dependabot supports neither. **Dependabot** keeps updating GitHub Actions only.

**Done when**

- [ ] `pixi install` of the `default` environment succeeds on all three platforms in CI (see §4).
- [ ] Every `CPMAddPackage` call pins a tag and a commit SHA.
- [ ] Renovate opens a test update for one pixi package and one CPM pin.
- [ ] `THIRD_PARTY_NOTICES` lists every dependency in §2.3.

### 3. Repository layout and targets

#### 3.1 Layout

Folders are organised component-first: each component owns its public headers, private sources, tests, benchmarks and bindings. This matches target-based CMake, makes a component's boundary a folder boundary, and lets a component move to its own repository later (D12).

```text
cmake/                      OstiaComponent.cmake, Dependencies.cmake (CPM pins), CPM.cmake
telemetry/                  rank 0, real code in M0
fabric/                     rank 1, M0: topology model + fixture replay (RFC-0003)
exchange/  runtime/  query/ ranks 2-4, placeholders in M0
  <component>/
    include/ostia/<component>/   public, installed: *.h = C ABI (ostia_<component>_*), *.hpp = C++
    src/                         private sources and headers, never installed
    tests/  bench/  python/
    CMakeLists.txt  README.md
tools/ci/                   check_layering.py, check_telemetry_macros.py
tools/bench/                ostia-bench driver, compare.py
bench/baselines/            committed baselines; bench/results/ is git-ignored
pixi.toml  pixi.lock  CMakeLists.txt  CMakePresets.json
```

`fabric/tools/topo-capture/` is defined by RFC-0003; `tools/rent/` and `infra/setups/` are defined by RFC-0004.

The exchange, runtime and query **placeholders** ship no public headers and no API: a `CMakeLists.txt`, a README pointing to their future RFC, and an empty INTERFACE target. They are not installed or exported, and `find_package(ostia COMPONENTS exchange)` fails with a message naming the RFC that will define it. RFC-0001 approves only the empty skeleton; each component's design needs its own RFC.

#### 3.2 Targets

```cmake
ostia_add_component(NAME fabric RANK 1 DEPENDS telemetry)
ostia_add_component(NAME exchange RANK 2 PLACEHOLDER)
```

`ostia_add_component` creates `ostia_<name>` with the alias `ostia::<name>`, sets `OUTPUT_NAME ostia-<name>` so the library is `libostia-<name>.so` (PRD), makes `include/` PUBLIC and `src/` PRIVATE, and adds the `OSTIA_BUILD_<NAME>` option. Installed consumers write `find_package(ostia COMPONENTS fabric)` and link `ostia::fabric`.

#### 3.3 Dependency table and enforcement

Components are ordered by **rank**: telemetry 0, fabric 1, exchange 2, runtime 3, query 4. Ranks order components; they are not the PRD's layers 1a, 1b, 2 and 3. The allowed direct dependencies are:

| Component | May depend on |
| --- | --- |
| telemetry | — |
| fabric | telemetry |
| exchange | fabric, telemetry |
| runtime | exchange, fabric, telemetry |
| query | runtime, exchange, telemetry |

The authority for this table is PRD D12 together with this RFC. Runtime uses fabric's topology directly (PRD, Layer 2); a follow-up aligns the PRD's "only the layer directly below" sentence with the table.

```mermaid
graph BT
    telemetry["telemetry (rank 0)"]
    fabric["fabric (rank 1)"] --> telemetry
    exchange["exchange (rank 2)"] --> fabric
    exchange --> telemetry
    runtime["runtime (rank 3)"] --> exchange
    runtime --> fabric
    runtime --> telemetry
    query["query (rank 4)"] --> runtime
    query --> exchange
    query --> telemetry
```

Enforcement has three parts, each checking **direct** edges only. Exposure through a lower-rank component's public interface (query seeing fabric types through runtime) is allowed.

1. **At configure time,** `ostia_add_component` rejects any `DEPENDS` entry that is not in the table. Disabling a component that another enabled component needs is a configure error naming the dependent.
2. **After all targets exist,** the top-level `CMakeLists.txt` walks each `ostia_*` target's direct `LINK_LIBRARIES` and fails on any edge not in the table. This catches a raw `target_link_libraries` call that bypasses the helper.
3. **In CI and pre-commit,** `tools/ci/check_layering.py` scans each component's direct `#include` lines. It fails on includes of a higher-rank component, on includes that reach into another component's `src/`, and on relative includes across component folders.

Every failure follows the error-message contract ([Failure handling](#failure-handling)), for example:

```text
error: fabric/src/probe.cpp:12 includes <ostia/runtime/driver.hpp>
  fabric (rank 1) may depend on: telemetry
  fix: move the code to runtime, or change fabric's DEPENDS through an RFC (RFC-0001 §3.3)
```

#### 3.4 Host-only boundary

The fabric **topology model and fixture replay** target is unconditional: it builds with no CUDA, NVML, ibverbs or UCX, so it works on macOS. Live discovery providers, the capture tool, UCX transport tests and GPU benchmarks are separate targets, enabled only on platforms and environments that have their dependencies. `OSTIA_ENABLE_CUDA` is `AUTO` by default, and `ON` fails at configure time with the detected and required versions.

#### 3.5 Python bindings

- Python packages share the `ostia` import namespace (PEP 420). Each component installs into `ostia/<component>/`, and no package ships `ostia/__init__.py`.
- **Development installs** are per-component scikit-build-core editable installs (scikit-build-core 1.0.1 or newer, which fixed shared namespace packages in redirect mode). One native build and install prefix per pixi environment and preset owns every `libostia-*.so`. Component editables link against that prefix and never build or bundle lower-rank native libraries. All extensions use the same pinned nanobind version.
- `pixi run py-dev` installs the editables in rank order with `--no-build-isolation`, a persistent build directory and the selected preset. Rebuilding after a native change is an explicit, documented command. Switching telemetry flavour rebuilds the whole stack or fails clearly. Only implemented components get editables; placeholders get none.
- Library lookup uses `$ORIGIN` on Linux and `@loader_path` on macOS.
- If these conditions cannot be met in PR 1, the fallback is one shared CMake install prefix with Python packages installed from it.

**Done when**

- [ ] All five component folders exist; the placeholders are neither installed nor exported, and `find_package` on one fails with the RFC pointer.
- [ ] A negative test proves each enforcement part fails: an upward `DEPENDS`, an upward raw link, an upward include and an include into another component's `src/`.
- [ ] A positive test proves a legitimate transitive dependency passes.
- [ ] From outside the checkout, one interpreter imports `ostia.telemetry` and `ostia.fabric`, no `ostia/__init__.py` is installed, and exactly one `libostia-telemetry` path is loaded.
- [ ] After a documented rebuild, a change to native code is visible from Python.

### 4. CI

#### 4.1 CPU jobs (GitHub-hosted, free for public repositories)

| Job | Runner | What it runs | When |
| --- | --- | --- | --- |
| `linux-x64-gcc11`, `linux-x64-gcc14` | ubuntu-24.04 | Build + unit tests; all four telemetry levels on `gcc14`, `metrics` + `debug` on `gcc11` | Every PR |
| `linux-x64-clang` | ubuntu-24.04 | Build + unit tests, `metrics` + `debug` | Every PR |
| `linux-arm64-clang` | ubuntu-24.04-arm | Build + unit tests | Every PR |
| `macos-arm64-host` | macos-15 | `default` environment resolve, build, unit tests, pytest | Every PR |
| `nvcc-12.8`, `nvcc-13` | ubuntu-24.04 | CUDA compile-only (GCC 11 / GCC 14) | Every PR |
| `container-ubuntu2204`, `container-rocky9` | ubuntu-24.04 | Build with distro packages, no pixi | Every PR |
| `sanitize-asan-ubsan`, `sanitize-tsan` | ubuntu-24.04 | Clang, `debug` level | Every PR |
| `multiprocess-tcp` | ubuntu-24.04 | Multi-process tests over UCX TCP loopback | Every PR |
| `lint` | ubuntu-24.04 | clang-format, ruff, gersemi, `check_layering.py`, `check_telemetry_macros.py`, `gen_index.py --check` | Every PR |
| `docs-as-test` | ubuntu-24.04, fresh | Runs `docs/guides/building.md`'s host-only commands verbatim; records the time taken | Every PR |
| `levels-full` | all | Full telemetry-level matrix on every configuration | Nightly |

- clang-tidy runs on changed files in CI and as a manual pre-commit stage, because it needs `compile_commands.json` and is slow.
- Required checks aim to finish within **20 minutes**. Jobs use per-PR concurrency groups with `cancel-in-progress`. Every job calls the same `pixi run` tasks a contributor runs locally (`pixi run check` is the required subset) and prints the command to reproduce a failure.
- ccache runs through pixi, with `actions/cache`.

#### 4.2 GPU jobs

**Runner.** An ephemeral AWS `g6.xlarge` (one L4, `sm_89`) per job, started by **Cirun**: it is free for public repositories and supports spot instances with fallback to on-demand. GitHub's own GPU runners were rejected: they are T4 (`sm_75`, below the architecture floor), and larger runners are not free for public repositories. RunsOn was rejected because an open-core company needs its commercial licence.

**Authorisation.** A pull request's `pull_request` workflow runs the workflow file from the PR's own merge commit, so a fork could edit it to skip any check it contains. GPU jobs therefore never run from `pull_request` workflows:

```mermaid
sequenceDiagram
    participant C as Contributor (fork)
    participant M as Maintainer
    participant GH as GitHub
    participant W as gpu.yml (default branch)
    participant R as Cirun L4 runner
    C->>GH: push to PR
    GH->>GH: label-reset workflow removes ci:gpu
    M->>GH: review diff, add ci:gpu
    GH->>W: workflow_run (default-branch file)
    W->>W: record PR number + head SHA at label time
    W->>M: environment "gpu" requires approval of that SHA
    M->>W: approve
    W->>R: request runner (pull_request: false in Cirun)
    R->>R: checkout SHA, persist-credentials: false, run tests
    C->>GH: new push
    GH->>W: cancel queued run, label removed, re-approval needed
```

- The GPU workflow lives on the default branch and is started by `workflow_run` on the label event or by a maintainer's `workflow_dispatch`. It receives the PR number and the head SHA captured at label time, runs in a GitHub environment named `gpu` with required reviewers so the approver sees that exact commit, and checks out that SHA with `persist-credentials: false`.
- Cirun's access control is set to `pull_request: false`, so no `pull_request` workflow can obtain a GPU runner even if a fork adds the runner label.
- A label-reset workflow on `pull_request_target` (`synchronize` only, no checkout, `permissions: pull-requests: write`) removes `ci:gpu` on every push. A push after labelling cancels the queued run.
- The repository requires approval for all external contributors' workflows.
- Pushes to `main` and the nightly schedule run GPU jobs without a label.
- Contributors without label rights ask a maintainer for `ci:gpu`. The PR shows one of four states: awaiting label, running, passed, failed. Every new push needs a new label.

**Isolation.** GPU CI runs in an AWS account separate from gate runs.
- The VM has no instance profile, uses IMDSv2 with a hop limit of 1, allows security-group egress on port 443 only, and is destroyed after one job.
- The workflow has minimal `permissions:` and passes no secrets to the job, and caches are read-only for PR runs, because `workflow_run` workflows otherwise get write tokens and cache access.
- Domain-level egress filtering (a proxy or AWS Network Firewall, about $290 a month per endpoint) is out of M0 scope.
- The Cirun app's permissions are listed in `MAINTAINERS.md`, and it is installed on this repository only.

**What GPU jobs run.**
- Unit and integration tests at all telemetry levels.
- Multi-process tests with 2 or more processes on one GPU (CUDA IPC + UCX loopback).
- `compute-sanitizer` (memcheck, racecheck, synccheck) nightly.
- Tests carry ctest labels `cpu`, `gpu` and `multiprocess`. GPU tests skip with an explicit reason when no device is present.
- A spot interruption retries once on on-demand. Infrastructure failures are reported as `infra-failed`, separately from test failures.

#### 4.3 GPU CI cost (estimates, prices checked 2026-09-25)

| Item | Price | Planned use | Estimate |
| --- | --- | --- | --- |
| `g6.xlarge`, us-east-1 | $0.63/h spot, $0.81/h on demand | Nightly (~45 min), pushes to `main` (~40 × 15 min/month), labelled PRs (~20 × 15 min/month): about 40 h/month | $25–32 per month |
| Cirun | Free for public repositories | — | $0 |
| **GPU CI sub-budget for M0** | | About 2.5 months, plus retries | **$200** |

The rented-hardware sub-budget ($1,000) and a $300 contingency make up the rest of the $1,500 (RFC-0004). The launcher refuses new GPU jobs once the GPU CI sub-budget is spent, and a maintainer decides at the re-scope review.

**Done when**

- [ ] Every job in §4.1 is green on `main`, and required checks finish within 20 minutes on a typical PR.
- [ ] The `docs-as-test` job passes and records its duration.
- [ ] A test fork PR that edits the workflow files cannot obtain a GPU runner without approval.
- [ ] A test step in the GPU job confirms that the instance metadata service is unreachable.
- [ ] Label-then-push cancels the queued run, and re-labelling runs the new SHA only.
- [ ] Nightly `compute-sanitizer` survives a simulated spot interruption through retry.
- [ ] Multi-process tests pass over UCX TCP loopback on CPU and with 2 processes on one L4.

### 5. Telemetry build levels

This section covers only how the level is chosen and compiled. The runtime is RFC-0002.

- A CMake cache variable, `OSTIA_TELEMETRY=off|metrics|trace|debug`, selects the level. Release presets default to `metrics`, the `dev` preset to `debug`. There is a preset per level.
- The build generates a `config.h` holding `OSTIA_TELEMETRY_LEVEL` (0–3). Dependent components get it through `ostia::telemetry_config`, an INTERFACE target that exists only in the build tree. It is never installed, and no public header includes it.
- The instrumentation macros `OSTIA_COUNT`, `OSTIA_TRACE_EVENT` and `OSTIA_DEBUG_CHECK` expand to `if constexpr (OSTIA_TELEMETRY_LEVEL >= N) { ... }`. Their arguments must be valid expressions and are **never evaluated** when the level is below `N`, so arguments must not have side effects. The macros work at function scope only.

```cpp
OSTIA_COUNT(bytes_sent, n);          // fine
OSTIA_COUNT(bytes_sent, pop_next()); // wrong: pop_next() does not run in an `off` build
```

- `tools/ci/check_telemetry_macros.py` fails if any header under `*/include/` uses these macros or includes `config.h`, which keeps public headers level-independent.
- Every flavour has the same soname, and a flavour applies to the whole installed stack. Each component records its compile-time level and checks it against `ostia_telemetry_build_level()` at initialisation; a mismatch fails immediately with both levels in the message.
- The fabric RFC must reserve a protocol-negotiation field before any telemetry level changes the message layout (trace IDs in `trace` builds, PRD).

**Done when**

- [ ] All four levels build; an `off` build contains no telemetry symbols (checked with `nm`).
- [ ] A compile test shows that a type error inside a disabled macro still fails to compile.
- [ ] Loading components built at different levels fails with a clear message.
- [ ] `check_telemetry_macros.py` fails on a planted macro in a public header.

### 6. Benchmark harness and the M0 gate

#### 6.1 Tools

- **nvbench** runs in-process benchmarks: kernels and single-process copies.
- **`ostia-bench`** (Python, in `tools/bench/`) drives multi-process and multi-node benchmark binaries built from each component's `bench/`. It converts nvbench JSON to the common schema.

#### 6.2 Result schema (version 1)

Results are JSON Lines, one record per measurement:

```json
{"schema": 1,
 "provenance": {"git_sha": "3f2a9c1", "date": "2026-11-02T10:14:00Z", "run_id": "nvlink-a100-20261102-01"},
 "compat": {"gpu": "A100-SXM4-80GB", "driver": "580.95", "cuda": "12.8", "nic": "none",
            "topology": "sha256:5d1e...", "build_level": "off", "compiler": "gcc-14 -O3", "deps": "pixi.lock:9ab3..."},
 "bench": "p2p_copy", "params": {"bytes": 1073741824, "direction": "0->1", "concurrency": 1},
 "unit": "GB/s", "higher_is_better": true,
 "samples": [44.1, 44.3, 44.2], "median": 44.2, "p5": 44.1, "p95": 44.3}
```

- **Provenance** fields (git SHA, date, run ID) never affect comparability.
- **Compatibility** fields decide whether two results may be compared. The topology hash is structural: it covers devices and links, not measured bandwidth.
- Tools reject unknown schema versions with an explanation.

#### 6.3 Comparison (`tools/bench/compare.py`)

- **Outcomes:** `pass`, `regression`, `inconclusive` and `invalid`. A required gate never passes on `inconclusive`.
- **Validity:** each run has a manifest of expected cases. Missing or duplicate cases, malformed records and non-finite samples make the run `invalid`, and so do missing or empty result files.
- **Compatibility rules** are defined per comparison. A baseline check requires equal compatibility fields. The overhead gate deliberately compares different build levels on the same box.
- **Regression rule:** a median worse by more than max(5%, 3 × MAD) in the metric's direction, with at least 10 samples per side. With fewer samples, or when the spread cannot resolve the threshold, the result is `inconclusive`.
- The job summary lists every skipped or inconclusive comparison.
- Baselines live in `bench/baselines/<setup>.json`. Updating one is its own pull request with a reason; `compare.py --write-baseline` produces its content.

#### 6.4 Gate workloads

The gate measures hardware ceilings and the prototype's techniques with **standalone reference programs**, not with Ostia's data path, which does not exist until M1.

| Workload | What it shows | Source | Oracle | Owner PR |
| --- | --- | --- | --- | --- |
| Calibration: P2P copy | NVLink/PCIe ceiling | `fabric/bench/p2p_copy.cu` | within 5% of `nvbandwidth` with matched size, direction, concurrency and memory placement | PR 5 |
| Calibration: RDMA put | GPUDirect RDMA ceiling | `fabric/bench/rdma_put.cpp` (UCX) | within 5% of `ib_write_bw` / `ucx_perftest` with matched parameters | PR 5 |
| Pipelining | Chunked, overlapped transfers vs synchronous | `fabric/bench/pipelining.cu` | received data checksummed | PR 5 |
| Batching | Message-size effect | `fabric/bench/batching.cu` | received data checksummed | PR 5 |
| Dual-link | Two paths at once | `fabric/bench/dual_link.cu` | received data checksummed | PR 5 |
| GPUDirect RDMA | GPU-to-GPU across nodes | `fabric/bench/gdr_put.cpp` (UCX) | received data checksummed | PR 5 |

The programs port the ideas of the prototype's micro-benchmarks; they do not copy its code (D6).

- **Evidence of transport.** Every gate run records evidence of the transport actually used (for example UCX transport names, the memory type of registrations, and which NICs or NVLinks carried traffic). A run that cannot show it used the capability it is testing fails instead of passing.
- **Capability profiles.** Each reference setup has a capability profile, chosen before the run, for the primary machine and its fallback (RFC-0004). An experiment the setup cannot support is reported as `unsupported`.
- **Expected bounds come from the box, not from fixed ratios.**
  - The pipelining bound is derived from the measured stage times: the transfer can hide the pack time only up to the slower stage.
  - The dual-link bound is the sum of the two links unless a shared resource (a PCIe switch, a NIC, host memory) caps it lower. That resource's measured limit is then the bound.
  - The GPUDirect RDMA bound is the NIC line rate measured by the reference tool.

**The M0 gate passes when:**
1. Both calibration workloads agree with their reference tools within 5% on the reference setups.
2. The experimental workloads reach at least 90% of their derived bounds, with transport evidence.
3. All of this holds on the primary setup, or on a pre-declared fallback that has the needed capabilities.

A missed target is triaged: re-run on a fresh box, compare against the reference tool, and inspect the compatibility fields. A target changes only through an ADR.

#### 6.5 The prototype's numbers (reference only)

The prototype's published figures (public repository `fardatalab/MGI`, formerly `Magi`) were measured on 4× V100-SXM2-16GB with bonded NVLink (NV2) and two mlx5 NICs. They are hardware-specific, so they calibrate expectations, not the gate.

| Figure | Result | Kind |
| --- | --- | --- |
| Opt1 pipelining | 22.4 → 44.2 GB/s | primitive |
| Opt2 batching | 2.1 GB/s (64 B) and 18.1 GB/s (1 KiB) → 43.9 GB/s | primitive |
| Opt3 | 43.9 → 58.0 GB/s (configuration not recorded in the notebook) | primitive |
| Opt4 dual-link | 43.9 → 90.2 GB/s | primitive |
| Opt5 GPUDirect RDMA | 80 → 100 Gbps (rounded in the source) | primitive |

"Primitive" means an isolated technique measured on its own, as opposed to an integrated data path or an end-to-end workload. The PRD notes that the prototype's RDMA library was never wired into its data path.

#### 6.6 Telemetry overhead gate

- PR 5 lands the measurement mechanism: interleaved A/B runs of the same benchmark on the same box, at least 10 interleaved pairs per level.
- The acceptance gate activates with RFC-0002's implementation, once real counters and export exist. At that point it states the instrumentation density and exporter state it measures, and it checks that the expected counters actually changed.
- The gate fails when `metrics`, or `trace` with tracing switched off, is more than 2% slower than `off` (PRD limits). The trace-on limit (10%) belongs to RFC-0002.
- The L4 runner's noise floor is measured and recorded. If it exceeds 2%, the gate runs on a quiet rented box instead, paid for from the contingency.

**Done when**

- [ ] `compare.py` has tests for each outcome, including the rejection of missing, duplicate and non-finite cases, and for comparing results from different commits on the same compatible box.
- [ ] An injected 3% slowdown fails the overhead mechanism's self-test, and 0% passes.
- [ ] Each gate workload in §6.4 exists with its oracle and records transport evidence.
- [ ] The gate passes on the three reference setups (RFC-0004), or on pre-declared fallbacks.
- [ ] `docs/guides/benchmarks.md` shows how to run a benchmark and update a baseline.

### 7. Topology fixtures (summary)

Topology fixtures are captured descriptions of real machines, scrubbed of identifiers, that let discovery and the planner be tested offline on any laptop. The planner is Ostia's edge, its bugs depend on hardware shape, and CI never has those shapes (D9). **RFC-0003** ([PR #8](https://github.com/OstiaHQ/ostia/pull/8)) designs the capture tool, the scrubbing rules, the fixture format and replay. It also owns the **capture artifact manifest**, the contract RFC-0004 relies on before fetching fixtures and destroying a rented machine.

**Done when**

- [ ] RFC-0003 is accepted and implemented (Rollout PR 6).
- [ ] `pixi run topo-show <fixture>` prints a captured machine's topology on macOS with the `default` environment.

### 8. Rented hardware (summary)

Gate benchmarks run on rented machines for the three D9 reference setups: a node with NVLink GPUs, a pair of nodes with GPUDirect RDMA NICs, and a TCP or EFA pair. **RFC-0004** ([PR #9](https://github.com/OstiaHQ/ostia/pull/9)) designs the one-command up/run/down tool, the enforced spend limits, the providers and fallbacks, credentials and quotas. It consumes RFC-0003's manifest contract. Its spend is the $1,000 rented-hardware sub-budget of the M0 budget.

**Done when**

- [ ] RFC-0004 is accepted and implemented (Rollout PR 7).
- [ ] The gate runs in §6.4 have been executed through it within the sub-budget.

### 9. Competitive landscape verification

The PRD's landscape table is "from memory and not yet checked". It is verified in a separate PRD pull request (Rollout PR 0), which may run while this RFC is in review.

**Acceptance criteria**

- Every row cites a primary source (project docs, repository or release notes) with the date it was checked.
- Every "(to verify)" is confirmed or the row is corrected, and the "not yet checked" sentence is removed.
- Newer candidates are assessed and added or dropped with one line each: Mooncake Transfer Engine, DeepEP, UCCL, NCCL's device API (with its experimental GPU-initiated networking), RAPIDS rapidsmpf, NVIDIA NIXL and Sirius.
- Each project that overlaps Ostia gets one line saying why Ostia still wins, or what scope change follows.
- The PR states a build-versus-extend conclusion: an independent fabric, a planner on an existing transport stack, or contributing to an existing exchange implementation.

**On-path placement results.** The PRD asks whether running an operator at a middle hop beats pushing it to the source. The prototype's public repository has code for four-GPU source, middle and destination variants (`micro_benchmarks/onpath/on_path_four_gpu_*`). The recorded results found cover only single-path runs with and without on-path processing, plus aggregation runs, so the source-versus-middle comparison is re-run in PR 7 on the NVLink setup, under equal resource budgets. It is a measurement recorded in the PRD, not a gate.

**Done when**

- [ ] The PRD landscape PR is merged and meets every criterion above.
- [ ] The placement re-run's results are recorded in the PRD's open questions.

## Failure handling

Every tool and configure check in M0 follows one **error-message contract**: the problem, the offending item with file and line, the rule that was broken, the exact command or change that fixes it, and the RFC section. `pixi run doctor` and the configure summary print the same environment facts: compiler, CUDA on or off and why, toolkit, architectures, telemetry level, enabled components and dependencies found.

| Failure | Detected by | What the maintainer or contributor sees | State left behind |
| --- | --- | --- | --- |
| Layering violation | Configure check, link walk, `check_layering.py` | Error naming the file, line, component rank and allowed dependencies | Nothing built |
| CUDA requested but missing | Configure (`OSTIA_ENABLE_CUDA=ON`) | Detected versus required versions | Nothing built |
| Dependency fetch fails (removed tag, conda outage) | Build | Error naming the pin; fix is a pin-bump PR | CI red |
| Telemetry flavour mismatch | Component initialisation | Both levels and library paths | Process exits |
| GPU runner unavailable or spot-interrupted | Cirun | `infra-failed`, retried once on demand | VM destroyed |
| Driver or CUDA mismatch on the GPU image | GPU job preflight | Fingerprint printed, job fails | VM destroyed |
| Benchmark results missing, empty or malformed | `compare.py` | `invalid`, with the missing cases | Artifacts kept |
| Comparison cannot resolve the threshold | `compare.py` | `inconclusive`, listed in the summary; required gates fail | Artifacts kept |
| Gate target missed | Gate run | Failed gate report; triage, and a target changes only by ADR | Results kept |
| Budget or 10-week checkpoint reached | Budget ledger (§4.3, RFC-0004) | New launches refused; re-scope review | Running jobs finish |
| Quota not granted | Provider (RFC-0004) | Fallback setup used | — |

## Observability

- The four build levels in §5, and the overhead gate in §6.6 enforcing the PRD's limits.
- Every CI job summary prints the environment fingerprint and the command to reproduce it. GPU jobs print the SHA they ran.
- Benchmark records carry provenance and compatibility fields (§6.2). The rented-run ledger is in RFC-0004.

## Performance

The M0 gate (§6.4) is the performance target: calibrated ceilings within 5% of the reference tools, and the experimental workloads at 90% or more of their derived bounds. The telemetry overhead limits (≤ 2% for `metrics` and for `trace` with tracing switched off) are enforced by §6.6. CI wall time targets 20 minutes for required checks.

## Testing

| Test | Runs on |
| --- | --- |
| Layering: upward `DEPENDS`, raw link, include, `src/` reach-in fail; transitive dependency passes | CPU CI |
| Placeholder not exported; `find_package` fails with the RFC pointer | CPU CI |
| `off` build has no telemetry symbols; disabled macros still type-check | CPU CI |
| Mixed-flavour load fails; macro in a public header fails the lint | CPU CI |
| PEP 420: two components in one interpreter, one telemetry library, no `ostia/__init__.py` | CPU CI (Linux and macOS) |
| Native change visible from Python after the documented rebuild | CPU CI |
| `compare.py` outcomes, manifests, non-finite values, cross-commit comparison | CPU CI |
| Overhead mechanism self-test (3% injected fails, 0% passes) | GPU CI |
| `docs-as-test`: building.md host-only commands on a fresh runner | CPU CI |
| macOS `default` environment resolves and builds without UCX, rdma-core or CUDA | CPU CI |
| Multi-process over UCX TCP loopback / 2 processes on one GPU | CPU CI / GPU CI |
| Fork PR editing workflows cannot reach a GPU runner; label/push race; IMDS unreachable | GPU CI (manual test PR, then kept as a regression check) |
| Spot interruption retry in the nightly sanitizer job | GPU CI |
| Gate workloads with oracles and transport evidence | Rented setups (RFC-0004) |

Fixture tests are listed in RFC-0003 and rented-hardware tests in RFC-0004.

## Alternatives considered

- **CUDA 13 only:** simpler matrix, but needs R580 drivers that clusters lag behind on. **CUDA 12.4 floor:** wider reach, but no Blackwell in the floor toolkit.
- **aarch64 as tier 1 from M0:** needs rented Grace Hopper runs in CI before there is code to test.
- **vcpkg:** no UCX package and awkward CUDA-coupled ports. **CPM for everything:** building UCX and protobuf from source on every clean CI run. **System packages only:** nothing pinned.
- **Kind-first layout** (`include/`, `src/` at the top): older convention for single-product repos; makes component boundaries and later splits harder.
- **GitHub GPU runners:** T4 is below the architecture floor, and they cost money for public repos. **RunsOn:** commercial licence required for open core. **Self-managed SkyPilot runner:** rebuilds what Cirun provides. **No GPU CI until M1:** leaves CUDA code untested in M0.
- **Label-gated `pull_request` GPU workflows:** a fork can edit the workflow and skip the gate.
- **All telemetry in RFC-0001:** doubles the size of this RFC; the runtime deserves its own review.
- **Custom-only harness or Google Benchmark:** re-implements nvbench's statistics or lacks GPU timing. **Wrapping nvbandwidth / ucx_perftest / nccl-tests as the harness:** they calibrate, but cannot run the pipelining and dual-link experiments or produce our schema; they remain the calibration references.
- **Gate on the prototype's absolute numbers:** needs hardware matched to 2020-era V100 machines.
- **One shared CMake prefix for Python** instead of per-component editables: simpler, and kept as the fallback in §3.5.
- **Domain egress filtering for GPU CI:** about $290 a month per endpoint, out of M0's budget.

## Rollout

| PR | Content | Implements |
| --- | --- | --- |
| 0 | Competitive-landscape PRD update; quota requests for the three reference setups | §9, RFC-0004 |
| 1 | Skeleton, CMake helper, presets, pixi, lint, `docs/guides/building.md`, README "Build from source" link, CONTRIBUTING setup link, `docs/guides/README.md` task index | §1–§3 |
| 2 | CPU CI, `docs-as-test`, `gen_index.py --check` in CI; update CONTRIBUTING.md's "docs checks are run by hand" and the `dependabot.yml` comment | §4.1 |
| 3 | Telemetry build levels | §5 |
| 4 | GPU CI | §4.2 |
| 5 | Benchmark harness, gate workloads, overhead mechanism | §6 |
| 6 | Topology fixtures | RFC-0003 |
| 7 | Rented-hardware tooling, gate runs, placement re-run | RFC-0004, §6.4, §9 |
| 8 | Telemetry runtime; activates the overhead acceptance gate | RFC-0002, §6.6 |

- **RFC-0002 is reserved for ostia-telemetry.**
- Each PR ships its how-to guide as a "Done when" item: adding a component (PR 1), running a benchmark and updating a baseline (PR 5), capturing a fixture (PR 6), running a rented setup (PR 7).
- Each RFC must be Accepted before its implementation PR starts.
- **Pre-release compatibility.** Result and fixture schemas carry a version, and tools reject versions they do not know. `docs/guides/building.md` has "update your checkout" steps: when to re-run `pixi install`, when to reconfigure, and how to remove generated state. Renamed pixi tasks or presets are listed in `CHANGELOG.md` with their replacement.

## Open questions

- What triggers C++23 beyond the stated floors (CUDA 13.3 and GCC 13): a specific library feature, or a date?
- When does aarch64 become tier 1: after the first Grace Hopper gate run, or after M1?
- Wording in the PRD: align "each layer uses only the contract of the layer directly below" with the dependency table in §3.3.
