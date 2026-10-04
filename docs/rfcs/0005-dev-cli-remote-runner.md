---
number: 5
title: ostia dev CLI and remote runner
status: Accepted
authors: [ShAlireza]
components: [build, docs]
created: 2026-09-29
updated: 2026-10-01
supersedes: []
superseded_by: []
discussion: https://github.com/OstiaHQ/ostia/pull/21
---

# RFC-0005: ostia dev CLI and remote runner

## Summary

This RFC designs `ostia-dev`, the one command-line tool for working on Ostia, and its `remote` subcommand, which runs Ostia's build and tests on machines other than the developer's own.

- **`ostia-dev`** gathers every contributor tool into one Python package with one entry point, one config file, one error-message contract and one exit-code contract. Build, test, lint, the repository checks, docs, benchmarks and the environment doctor all become subcommands (§1, §2).
- **`ostia-dev remote k8s`** runs a build and a test command in temporary pods on any Kubernetes cluster (GKE, EKS, AKS or another), on GPU or CPU nodes, selected by kube context and node profile. It streams the output, copies the results back and tears everything down, including when the run fails, is interrupted or the CLI crashes (§3, §4).
- **`ostia-dev remote container`** runs the same pipeline in a local podman or docker container. It replaces `check-cuda` (§5).

```bash
pixi run ostia-dev remote k8s --context gcp-us-central1-intuigence --profile l4 -- ctest -L gpu
```

creates a pod on an L4 node of that GKE cluster, builds Ostia from the local working tree (uncommitted changes included), runs the GPU tests, prints the results, exits with the test result and deletes the pod.

The remote runner replaces the automated GPU CI of RFC-0001 §4.2 and §4.3. That decision, and what it does to RFC-0001's "Done when" items, is recorded in [ADR-0014](../adr/0014-on-demand-remote-test-runs.md).

## Motivation

RFC-0001 §4.2 designed GPU CI as ephemeral AWS `g6.xlarge` runners started by Cirun, with label gating, approval environments, a separate AWS account and an AWS Budgets action (§4.3). The maintainer has since dropped automated GPU CI (ADR-0014). The reasons in short:

- Ostia has one maintainer, who already has Kubernetes clusters with GPU nodes. A separate CI account, Cirun, the label/push race handling and the budget action are a lot of machinery to guard against untrusted fork code, and none of it is needed when a maintainer runs reviewed code on demand.
- GPU tests need to run on more than one GPU type (L4 today; A100 and H100 when available) and sometimes on two nodes (the UCX programs of RFC-0001 §6.4). A fixed `g6.xlarge` runner covers one of those.
- The maintainer works on macOS without CUDA (CLAUDE.md, Environment notes). The loop that matters is "edit on the Mac, run on a GPU, read the result", without pushing first.

At the same time, Ostia's contributor tooling has grown as separate scripts and pixi tasks: `tools/ci/*.py`, `tools/dev/*.py`, `tools/docs/*`, `tools/bench/*` (#18, #19), each with its own argument parsing and error printing. A remote runner needs config, credentials handling, profiles and a results contract, so it can't be yet another script. This RFC therefore defines the shared CLI first and puts the runner inside it.

The RFC rule in `docs/README.md` applies: this adds third-party dependencies (typer, kubectl, git), handles cluster credentials, and replaces RFC-0001 §4.2.

## Goals and non-goals

**Goals**

- Every contributor uses one tool, `ostia-dev`, for day-to-day Ostia development, with `--help` listing every task (§1, §2).
- The maintainer runs any Ostia test, including GPU and two-node tests, on any Kubernetes cluster from a Mac, with local uncommitted changes, in one command (§3, §4).
- Nothing stays behind: every run's cluster objects are deleted on success, failure, Ctrl-C and a crashed CLI, without relying on the CLI surviving (§4.8). The two deliberate exceptions are a namespace the tool created on request and the opt-in `--cache` volume, which persist until `cleanup` removes them.
- Runs on shared clusters are isolated by default: no service-account token, no cloud identity, Pod Security `restricted`, default-deny networking (§4.6).
- A run's exit code is the test result, and its results are on the Mac afterwards in a fixed layout (§3.4, §3.5).
- Contributors without a GPU or a cluster can still check CUDA code locally (§5) and get GPU-affecting pull requests tested by a maintainer (ADR-0014).

**Non-goals**

- Automated GPU CI on pull requests. Dropped by ADR-0014.
- Running unreviewed code. The runner executes whatever it is given; the developer is responsible for having read it. Isolation protects the cluster's other workloads, not the run from its author. Contributor code is allowed only after review, with extra limits (§4.10).
- Long-lived clusters or cluster provisioning. The runner uses clusters that already exist; it creates namespaced objects only (plus a namespace on request).
- The `ssh` and `rent` backends. §3.1 defines the interface they will implement; each gets its own RFC or ADR.
- Publishing `ostia-dev` to PyPI. It is contributor tooling and is never uploaded.

## Design

### 1. The `ostia-dev` CLI

#### 1.1 Package and entry point

- The CLI is a Python package at `tools/ostia-dev/`, with its own `pyproject.toml`: distribution `ostia-dev`, src layout `src/ostia_dev/`, classifier `Private :: Do Not Upload`, and a `[project.scripts]` entry point `ostia-dev = "ostia_dev.cli:app"`.
- pixi installs it editable in **every** environment through `[pypi-dependencies] ostia-dev = { path = "tools/ostia-dev", editable = true }`. It is therefore available on the Mac, in CI and inside remote pods, and `pixi run ostia-dev …` or plain `ostia-dev …` inside `pixi shell` both work.
- The import name `ostia_dev` stays outside the product's `ostia.*` namespace (PEP 420, PRD "Python package"). The command is `ostia-dev`, not `ostia`, so a future user-facing `ostia` command from `pip install ostia` has its name free.
- Arguments are parsed with **typer**. Commands form groups: `ostia-dev <group> <verb>`, plus top-level verbs for the everyday tasks (§2). typer's built-in shell completion (`ostia-dev --install-completion`) is documented, not extended.

#### 1.2 Config

- Format: TOML, read with the standard library's `tomllib`. Every file carries `schema = 1`; an unknown schema is rejected with the supported versions.
- Built-in defaults ship in the package (`ostia_dev/remote/profiles.toml`: node profiles and suites, §4.4 and §3.3).
- The user file is `~/.config/ostia/config.toml` (XDG; `OSTIA_CONFIG` overrides the path). It holds cluster contexts and profile overrides and additions. Keys merge per key over the built-ins.
- The repository never stores cluster names, contexts or credentials.

```toml
# ~/.config/ostia/config.toml
schema = 1

[remote.k8s.contexts.gcp-us-central1-intuigence]
provider = "gke"                # gke | eks | aks | generic
namespace = "ostia-test"        # default for this context; --namespace overrides it
kubectl = "/usr/local/bin/kubectl"   # optional; defaults to the pinned kubectl (§4.1)

[remote.k8s.profiles.l4.gke]    # override one built-in field
ephemeral_storage = "80Gi"
```

The CLI can write this file for you on first use (§4.3); it never writes it without asking.

#### 1.3 Error messages

Every error follows RFC-0001's error-message contract: the problem, the offending item, the rule that was broken, the exact fix and the RFC section. The shape is the one `tools/ci/_contract.py::violation()` already prints; that helper moves into `ostia_dev` (§2.3) and every subcommand uses it. For example:

```text
error: namespace ostia-test has no ResourceQuota
  context: gcp-us-central1-intuigence
  rule: runs need the guardrails of RFC-0005 §4.6 (PSA restricted, ResourceQuota, NetworkPolicy)
  fix: ostia-dev remote k8s init --context gcp-us-central1-intuigence --namespace ostia-test
       or rerun with --allow-unguarded (recorded in the run summary)
  see: RFC-0005 §4.3
```

`-v/--verbose` prints every external command (kubectl, podman, git) with its arguments, for debugging.

#### 1.4 Exit codes

For every subcommand: **0** success, **1** the checked thing failed (tests, lint, a check), **2** usage or configuration error. `remote` adds three (§3.5). *(update: Rollout PR B: commands that wrap an external tool (`build`, `test`, `check`, `check graph|macros|tidy`, `py-dev`, `hooks`) exit with that tool's own code, as the pixi tasks they replace did, so a failing ctest still exits 8; §2.3 allows only names and paths to change.)* *(update: Rollout PR 6b: `topo capture` passes the capture tool's exit code through: 0 complete, 2 partial, 1 failed, 3 leak or schema violation, so 2 here means a partial capture rather than a usage error (#45).)*

#### 1.5 Prompts and non-interactive use

The CLI prompts only on a TTY. Every prompt has a flag equivalent (`--yes`, `--namespace`, …). Without a TTY, a question the flags don't answer is an exit 2 with the fix printed, so scripts never hang.

### 2. Subcommands and migration

#### 2.1 Target layout

```text
tools/ostia-dev/
  pyproject.toml
  src/ostia_dev/
    cli.py            # typer app, top-level verbs
    contract.py       # error-message contract (from tools/ci/_contract.py)
    config.py         # §1.2
    dev/              # build, test, py-dev, clean, doctor, hooks
    ci/               # lint, fmt, check, check layering|graph|macros|cpm-pins|exports, docs-as-test
    docs/             # docs index, docs figures
    bench/            # bench run|convert|compare|overhead (from #18, #19)
    remote/           # core.py (§3), k8s/ (§4), container.py (§5), profiles.toml, pod/ (supervisor, §4.5)
  tests/
```

`telemetry/tools/gen_catalog.py` stays where it is: CMake runs it at build time as code generation, and it belongs to the telemetry component. `tools/ci/install_cmake.sh` and `tools/ci/gpu_preflight.sh` (#17) stay shell scripts, because they run before pixi exists (the container CI jobs, and inside remote pods). *(update: Rollout PR B: for the same reason, four moved modules stay runnable by path with plain `python3` and the standard library: `ci/check_exports.py` and `ci/check_graph.py` (CMake tests, which the container CI jobs run without pixi), `ci/docs_as_test.py` (CI's fresh-runner job, which must not install the environment before the guide does) and `ci/check_comments.py` (the Claude Code hook). Their commands are still `ostia-dev check exports|graph|docs-as-test|comments`.)*

#### 2.2 Everyday commands: one vocabulary

Every user-facing command becomes `ostia-dev …`. pixi keeps only internal tasks whose names start with `_`, where its task graph needs them (for example `_configure`); `ostia-dev` sequences the rest itself.

Every command below runs as `pixi run ostia-dev …`, or as plain `ostia-dev …` inside `pixi shell`.

| Today | After the migration |
| --- | --- |
| `pixi run build` | `ostia-dev build` |
| `pixi run test` / `test-cpp` / `test-py` | `ostia-dev test` / `test cpp` / `test py` |
| `pixi run test-preset <p>`, `test-levels` | `ostia-dev test --preset <p>`, `ostia-dev test --levels` |
| `pixi run test-rebuild`, `test-multiprocess` | `ostia-dev test rebuild`, `ostia-dev test -L multiprocess` |
| `pixi run check` | `ostia-dev check` |
| `pixi run lint`, `fmt` | `ostia-dev lint`, `ostia-dev fmt` |
| `pixi run check-graph`, `check-macros`, `tidy` | `ostia-dev check graph`, `check macros`, `check tidy` |
| `tools/ci/check_layering.py`, `check_cpm_pins.py`, `check_exports.py` | `ostia-dev check layering`, `check cpm-pins`, `check exports` |
| `pixi run sanitize-asan`, `sanitize-tsan` | `ostia-dev test --sanitize asan-ubsan`, `--sanitize tsan` |
| `pixi run doctor`, `clean`, `py-dev`, `hooks` | `ostia-dev doctor`, `clean`, `py-dev`, `hooks` |
| `pixi run docs-index`, `python3 tools/docs/gen_index.py [--check]` | `ostia-dev docs index [--check]` |
| `bun tools/docs/render-figures.js …` | `ostia-dev docs figures` (still needs bun) |
| `pixi run docs-as-test` | `ostia-dev check docs-as-test` |
| `pixi run check-cuda [env]` | `ostia-dev remote container --env cuda-12 --env cuda-13 --preset release --no-test` (§5; `--env` repeats) |
| `pixi run bench …`, `tools/bench/*.py` (#18, #19) | `ostia-dev bench run|convert|median-seconds|compare|overhead` |
| `pixi run rent …` (RFC-0004, Rollout PR 7) | `ostia-dev rent …`, and later a `remote` backend (§3.1) |
| `pixi run topo-show`, the capture tool (RFC-0003, Rollout PR 6) | `ostia-dev topo show|capture` |

*(update: Rollout PR 6a: `ostia-dev topo show|golden [--update]` is new (`topo capture` follows in PR 6b), and `ostia-dev check fixture-leaks` is a new lint check (#32).)*

*(update: Rollout PR B: the migration also names what the table leaves out. `install-native` is part of `py-dev`. `check comments [FILES|--hook]`, `check graph --dot FILE`, `check macros --public|--build DIR` and `check tidy [FILES]` take the old scripts' arguments, and `lint` and `fmt` keep `--root` and `--only`. `bench oracles`, `bounds`, `capabilities` and `evidence` are the other benchmark scripts. `docs figures` defaults to the PRD's figures. `test -L <label>` runs ctest only, as `test-multiprocess` did. `check-cuda` becomes `ostia-dev remote container --env cuda-12 --env cuda-13 --suite cuda-compile`, because `--preset release --no-test` would not compile the benchmarks that `check-cuda` compiled (§3.3).)*

The quick start in `docs/guides/building.md` becomes `pixi install`, `pixi run ostia-dev build`, `pixi run ostia-dev test`. This amends the wording of RFC-0001's first goal ("three commands"), not its intent.

#### 2.3 How the migration lands

- **Implementation PR A** adds the package, the framework (§1), `remote` (§3–§5) and the kind CI job (Testing). The existing tools stay where they are, untouched.
- **Implementation PR B**, after #17–#19 have merged so they don't conflict, moves the existing modules into `ostia_dev/{dev,ci,docs,bench}/` and removes the old pixi task names and script paths in one step (a **hard switch**, no aliases). The same PR updates CI (`.github/workflows/*.yml`), `.pre-commit-config.yaml`, `CLAUDE.md`, `CONTRIBUTING.md`, `docs/guides/building.md`, `docs/guides/README.md` and `docs-as-test`, and `CHANGELOG.md` lists every old name with its replacement (RFC-0001 Rollout, pre-release compatibility).
- **Regression proof for PR B:** the existing tests move with their modules and pass with import changes only. A parity test runs each old entry point and its new subcommand on the same fixtures and compares exit code, stdout, stderr and files written. It lands in a commit before the old paths are deleted. `ostia-dev check` and `docs-as-test` pass on the PR. The intended differences are names and paths only.

#### 2.4 New dependencies

This RFC is the approval that `docs/README.md` requires.

| Dependency | Licence | Purpose | Source | Environments |
| --- | --- | --- | --- | --- |
| typer 0.27 (with rich, shellingham, annotated-doc, colorama) *(update: Rollout PR A; click is vendored inside typer and no longer a separate dependency)* | MIT AND BSD-3-Clause; MIT; ISC; MIT; BSD-3-Clause | Argument parsing, help, completion | conda-forge | all |
| kubectl, official release binary *(update: Rollout PR A; not conda-forge's `kubernetes-client`, which was 1.34 on linux-64 and osx-arm64 and 1.24 on linux-aarch64, outside the version-skew policy for 1.36–1.37 servers)* | Apache-2.0 | Talking to clusters (§4.1) | dl.k8s.io, a pinned version and per-platform sha256, downloaded and verified by ostia-dev on first use | all |
| git | GPL-2.0 (tool only, not linked) | CPM fetches source dependencies by `GIT_TAG` inside pods, which run as non-root and cannot install packages (§4.5) | conda-forge | all |
| setuptools *(update: Rollout PR A)* | MIT | Build only: the build backend of the `ostia-dev` editable, installed from conda-forge and used without build isolation, so nothing comes from PyPI at install time | conda-forge | all |
| kind *(update: Rollout PR A)* | Apache-2.0 | CI only: a local cluster for the `remote.yml` kind job (Testing) | conda-forge (`kubernetes-kind`) | `remote-ci` (CI only) |

PR A measures whether `typer-slim` (without rich) is enough; if it is, that is used instead and the table is updated in the PR. *Update (Rollout PR A): measured. `typer-slim` 0.24 is a shim that depends on `typer` itself, on conda-forge and on PyPI alike, so choosing it drops nothing; PR A uses plain `typer` (`>=0.27,<0.28`). conda-forge's `typer` depends on colorama on every platform, though typer only uses it on Windows.* Cloud credential plugins (`gke-gcloud-auth-plugin`, `aws`, `kubelogin`) are not dependencies: kubectl uses whatever the developer's kubeconfig names, and the guide lists them per provider.

*(update: Rollout PR 6b: `jsonschema` (MIT, conda-forge, all environments) is added for the consumer-side validation of `manifest.json` against `manifest.schema.json`, which RFC-0003 §4 requires (#45).)*

### 3. Remote runs

#### 3.1 Backends

A backend implements seven operations: `prepare` (validate config, preflight), `upload` (put the code in place), `start`, `stream` (output and status), `collect` (copy results), `teardown` and `gc` (remove anything left by earlier runs). The pipeline (§3.2), results (§3.4) and exit codes (§3.5) are shared.

| Backend | Designed in | Where the run happens |
| --- | --- | --- |
| `k8s` | §4 | Pods on a Kubernetes cluster |
| `container` | §5 | A local podman or docker container |
| `ssh`, `rent` | later (Open questions) | A machine reached over SSH; a RFC-0004 rented setup |

Common flags for every backend:

| Flag | Default | Meaning |
| --- | --- | --- |
| `--env` | `cuda-12` on GPU profiles, `default` otherwise | pixi environment; may repeat, running the pipeline once per environment |
| `--preset` | `dev` | CMake preset |
| `--suite` | none | A named suite (§3.3) instead of a command |
| `-- <command>` | `ctest --test-dir $OSTIA_BUILD_DIR --output-on-failure -j <profile CPUs>` | The command to run after the build |
| `--no-build` | off | Skip configure and build (the command builds, or needs no build) |
| `--no-test` | off | Stop after the build (compile-only runs) |
| `--timeout` | 60 min | Limit for the pipeline inside the pod |
| `--ref <sha>`, `--ref pr/<n>` | none | Run a pushed commit or a pull request's head instead of the working tree (§4.10) |
| `--env-var KEY=VALUE` | none | Pass one variable (§4.10) |
| `--allow-secret` | off | Allow a secret-looking `--env-var` (§4.10) |
| `--results <dir>` | `build/remote/` | Where results land locally |
| `--yes` | off | Answer yes to confirmations (§1.5) |
| `-v`, `--verbose` | off | Print every external command (§1.3) |

`k8s` only:

| Flag | Default | Meaning |
| --- | --- | --- |
| `--context` | required | kube context (§4.1) |
| `--namespace` | the context's configured default | Namespace (§4.3) |
| `--profile` | required | Node profile, for example `l4`, `a100`, `cpu` (§4.4) |
| `--pods 2`, `--same-node` | 1, off | Two-pod runs and their placement (§4.11) |
| `--schedule-timeout` | 20 min | How long to wait for pods to start (§4.5) |
| `--cache` | off | Mount the download cache (§4.7) |
| `--keep-on-failure[=N]` | off (30 min when given) | Keep a failed pod for debugging (§4.8) |
| `--allow-unguarded` | off | Run in a namespace without guardrails (§4.3) |
| `--kubectl PATH` | the pinned kubectl (§4.1) | Another kubectl binary (§4.1) |

`container` only: `--gpus` (pass the host's NVIDIA GPUs through, §5) and `--engine podman|docker` (default: whichever is found first). *Update (Rollout PR A): `container` also takes `--profile` (default `cpu`). `--gpus` needs a GPU profile, for example `--profile l4` on an L4 workstation, which supplies the compute capability, the GPU preflight and `OSTIA_REQUIRE_GPU`; a GPU profile without `--gpus` is an exit 2. The run ID is `container-<profile>-…`.*

#### 3.2 Pipeline

Every run executes the same steps, in the pod or container, under the in-pod supervisor (§4.5):

1. **Unpack** the uploaded tree into the work volume.
2. **Preflight.** GPU profiles run `tools/ci/gpu_preflight.sh` (driver ≥ R580, fingerprint) and the compute-capability check (§4.9).
3. **Install:** `pixi install --locked -e <env>`.
4. **Configure and build** the preset (skipped with `--no-build`).
5. **Command:** the command after `--`, run with `pixi run -e <env>` **in the preset's build directory**, with `OSTIA_BUILD_DIR` and `OSTIA_SOURCE_DIR` exported. That is why `-- ctest -L gpu` works as written. ctest always gets `--output-junit`.

The supervisor sets `CMAKE_BUILD_PARALLEL_LEVEL` and `CTEST_PARALLEL_LEVEL` from the profile's CPU and memory (one job per CPU, at most one nvcc job per 4 GiB), because ninja, nvcc and ctest would otherwise see every CPU on the node, not the pod's limit. Both values go into `summary.json`. An out-of-memory kill is reported as exit 3, naming memory and the profile key.

Each step writes its exit code and duration to `/w/.ostia/steps.json`. A failing preflight step is an infrastructure failure (exit 3). Three guards turn "tested nothing" into a failure:

- `CTEST_NO_TESTS_ACTION=error` is set in the pod, and a ctest run whose `junit.xml` holds zero tests fails with exit 1;
- on GPU profiles, a `gpu`-labelled test reported as skipped or not run fails the run with exit 3, because it means the GPU wasn't usable in the process;
- on GPU profiles the pod sets `OSTIA_REQUIRE_GPU=1`, which Ostia's GPU tests honour by failing instead of skipping when no device is found (added in the #17 rework, ADR-0014). Elsewhere, RFC-0001 §4.2's rule still holds: GPU tests skip with an explicit reason when no device is present.

#### 3.3 Suites

Suites are named step lists in `profiles.toml`. They reproduce what #17 and #18's GPU workflow ran, so nothing is lost when that workflow is dropped:

| Suite | Runs |
| --- | --- |
| `gpu` | preflight; the `dev` preset's `native` check (the summary line in `ostia-summary.txt`); build and full ctest at telemetry levels `off`, `metrics`, `trace`, `debug` (`level-*` presets with the node's architecture, §4.9), each writing `junit-<level>.xml`, with the guards of §3.2 applied to each |
| `sanitizer` | `compute-sanitizer --tool memcheck|racecheck|synccheck --error-exitcode 1` on the GPU test binaries of a `level-debug` build |
| `bench-smoke` | the benchmark driver end to end (nvbench to schema 1, RFC-0001 §6.1) and every `fabric/bench` program with `--smoke` |
| `overhead-aa` | the overhead mechanism's self-test, which fails the run on a wrong verdict, and the A/A noise-floor run on the node (RFC-0001 §6.6), which reports its noise floor and never fails the run |
| `cpu` | build and `ctest -L cpu` |

*(update: Rollout PR B: a sixth suite, `cuda-compile`, configures `release` with `-DOSTIA_BUILD_BENCH=ON` and builds it, with no tests and no GPU, so it runs on a CPU profile or a Mac's container; it replaces `check-cuda` (§2.2).)*

#### 3.4 Results

Results are copied to `build/remote/<run-id>/` (git-ignored under `/build/`):

```text
build/remote/k8s-l4-20261002-141501-a1b2c3/
  summary.json        # run ID, backend, context, namespace, profile, node, GPU, git SHA, tree hash,
                      # upload size, per-step exit codes and durations, node-hours, cost estimate, exit code
  log.txt             # the full streamed output
  fingerprint.txt     # gpu_preflight.sh output
  ostia-summary.txt   # the configure summary
  junit.xml           # or junit-<level>.xml for the gpu suite
  Testing/            # ctest's own output
  rank-0/ rank-1/     # the same, per pod, for two-pod runs (§4.11)
  capture/            # a topology capture, when a pod wrote /w/capture/manifest.json (RFC-0003 §4)
```

Benchmark output is also copied to `bench/results/<run-id>/`, evidence included, where `compare.py` expects it (RFC-0001 §6.2). Only an allowlist of paths is copied back. Symlinks and anything past the 2 GiB cap are dropped with a warning listing them; the run keeps its exit code. The run ID is `<backend>-<profile>-<UTC yyyymmdd-HHMMSS>-<6 hex>`. *(update: Rollout PR 6b: `capture/` holds one pod's capture, or `node-0/`, `node-1/` and `pair.json` for two pods, plus `status.json` (per node: accepted, rejected or absent, the status and the reason), and `summary.json` has a `capture` entry. The runner fetches the manifest first, then only the files it lists, before teardown; a capture never changes the run's exit code. The `topo-capture` suite is an ordinary command step, so a failed capture fails that suite, while a gate's capture step only reports (#45).)*

At the end the CLI prints one summary line, which is also what goes into a pull request (ADR-0014):

```text
k8s-l4-20261002-141501-a1b2c3  passed  L4 (8.9) driver 580.95  sha 3f2a9c1+dirty(tree 9ab3…)  suite gpu  18m12s
  upload 4s · node 3m40s · install 5m02s · build 6m31s · test 2m55s
  tip: install and build took 64% of this run; --cache reuses downloads between runs (RFC-0005 §4.7)
```

The per-step durations are always shown. The tip appears when install and build dominate.

#### 3.5 Exit codes

| Code | Meaning | Cluster state afterwards |
| --- | --- | --- |
| 0 | Every step passed | Deleted and verified |
| 1 | The tests or the command failed | Deleted and verified |
| 2 | Usage or configuration error | Nothing was created |
| 3 | Infrastructure failure: unschedulable, image pull, eviction, preemption, deadline, lost connection; the test result is unknown | Deleted and verified |
| 4 | The tests passed, but teardown could not be verified | Objects may remain; the `cleanup` command is printed |
| 130 | Interrupted by Ctrl-C | Deleted and verified; after a second Ctrl-C, delete requested but not verified |

The test result wins: a failed run whose teardown also can't be verified exits 1, not 4. `summary.json` records the code, the failing step and `teardown: verified | unverified`, and the CLI prints the `cleanup` command whenever teardown is unverified.

### 4. The `k8s` backend

#### 4.1 Talking to the cluster

- The backend drives a pinned `kubectl` *(update: Rollout PR A: the official release binary, v1.36.5 at first, downloaded to `~/.cache/ostia/kubectl/<version>/` and checked against the sha256 table in `ostia_dev/remote/k8s/kubectl.toml`; Renovate bumps it)* as a subprocess with generated manifests (`apply -f -`, `wait`, `logs -f`, `exec -i`, `delete`). It uses the developer's kubeconfig and credential plugins unchanged, and has kubectl's proven tar-over-exec path. There is no Python Kubernetes client. All calls go through one thin `Kube` wrapper, which is what the unit tests replace (Testing).
- `--context` is required on every run. The tool never falls back to kubectl's current context, so a run can't land on the wrong cluster.
- kubectl errors are mapped, not passed through raw: `Forbidden` names the missing verb and resource and the Role that grants it (§4.3); a missing or expired credential plugin names the plugin and its login command for the provider. Both are exit 2.
- Preflight reads `kubectl version -o json`. If client and server are more than one minor version apart (outside Kubernetes' version-skew policy), it prints a warning naming both versions and the fix: a newer pin, or `--kubectl PATH` / `kubectl = "…"` in the config.

```text
ostia-dev remote k8s [run flags] -- <command>   # a run (§3)
ostia-dev remote k8s init     --context C --namespace N [--privileged]   # create or complete a namespace's guardrails
ostia-dev remote k8s verify   --context C --namespace N                  # probe the isolation (§4.6)
ostia-dev remote k8s cleanup  --context C --namespace N [--run-id R | --all] [--delete-namespace]
ostia-dev remote k8s profiles [--context C]                             # list profiles, their selectors and resources
ostia-dev remote k8s usage                                              # node-hours and cost estimate from local run records
ostia-dev remote gate <setup>                                           # a gate run on a setup's k8s machine (§6)
```

#### 4.2 Workload: a Job

Each run is a `batch/v1` Job:

- `backoffLimit: 0` (never retried), `restartPolicy: Never`, `activeDeadlineSeconds` (§4.8) and `ttlSecondsAfterFinished: 600`, so the TTL-after-finished controller deletes the finished Job and its pods even if the CLI is gone.
- Two-pod runs use an **Indexed Job** (`completionMode: Indexed`, `completions: 2`, `parallelism: 2`) with a headless Service as its subdomain (§4.11).
- Per-run objects (the headless Service and the per-run NetworkPolicy) carry an `ownerReference` to the Job, so deleting the Job deletes them. An ownerReference needs the Job's UID, so the Job is created with `suspend: true`, the owned objects are created next, and then the Job is unsuspended. No pod runs before its policy exists.
- **Admission failures:** a pod the cluster refuses to create (ResourceQuota exceeded, a PSA violation, a policy webhook such as Gatekeeper or Kyverno, a missing ServiceAccount) never appears; the Job controller records `FailedCreate` instead. The CLI watches the Job's events and stops at the first `FailedCreate` with exit 2, printing the admission message, without waiting for the schedule timeout.

```mermaid
sequenceDiagram
    participant CLI as ostia-dev (Mac)
    participant API as API server
    participant Pod as Pod (supervisor)
    CLI->>API: preflight (context, namespace, guardrails, kubectl skew)
    CLI->>API: gc: delete this owner's expired runs
    CLI->>API: create Job (suspend: true)
    CLI->>API: create Service + NetworkPolicy (ownerReference: Job)
    CLI->>API: unsuspend Job
    API-->>CLI: events (FailedScheduling, TriggeredScaleUp, Pulling)
    Pod->>Pod: wait for code (at most 10 min)
    CLI->>Pod: exec -i tar -x (working tree)
    Pod->>Pod: preflight, pixi install, build, command
    Pod-->>CLI: logs -f (resumed on drop)
    CLI->>Pod: exec tar -c (allowlisted results)
    CLI->>Pod: touch /w/.ostia/collected
    Pod->>Pod: exit with the test's code
    CLI->>API: delete Job (cascades), wait until gone
```

#### 4.3 Namespaces

- The namespace comes from `--namespace`, or from the context's `namespace` in the user config. On a TTY with neither, the CLI asks `namespace for context <name>? [ostia-test]` and then `save as this context's default? [Y/n]`, and carries on. Without a TTY that is an exit 2 with the fix.
- **Missing namespace:** on a TTY the CLI asks `create namespace ostia-test? [y/N]` (`--yes` in scripts). It creates it with the guardrails of §4.6 and the label `ostia.dev/managed=true`. A created namespace is kept and reused; `cleanup --delete-namespace` deletes a namespace only if it carries that label.
- **Existing namespace without guardrails:** the preflight reads the namespace's PSA `enforce` label, its ResourceQuota and a NetworkPolicy that selects the run's pods. If any is missing, the run is refused with an error naming what's missing (§1.3). The fix is `init` (which adds them) or `--allow-unguarded`, which is recorded in `summary.json` and printed in the summary. The pod-level settings of §4.6 apply regardless.
- **Unconfigured context:** on a TTY the CLI reads one node's labels (`cloud.google.com/gke-nodepool`, `eks.amazonaws.com/nodegroup`, `karpenter.sh/nodepool`, `kubernetes.azure.com/agentpool`), shows the provider it detected, asks `save [remote.k8s.contexts.<name>] provider = "gke" to ~/.config/ostia/config.toml? [Y/n]`, writes it and carries on. Without a TTY it prints the line to add and exits 2. It never guesses silently. `generic` clusters need an explicit `node_selector` in their profile.

`init` creates, and prints, the exact access a developer needs, so nobody has to work it out:

- a namespaced Role `ostia-test-developer`: `create`, `get`, `list`, `watch`, `patch` and `delete` on Jobs; `get`, `list`, `watch` and `delete` on pods; `create` on `pods/exec`; `get` on `pods/log`; `create`, `get` and `delete` on Services and NetworkPolicies; `get` and `list` on events, ResourceQuotas and LimitRanges; `create`, `get` and `delete` on PersistentVolumeClaims (for `--cache`); `get` on the ServiceAccount `ostia-test-runner`; *(update: Rollout PR A: also `create`, `get` and `delete` on Secrets, for §4.10's per-run Secret, and `list` on NetworkPolicies, for the preflight's check that a policy selects the run's pods)*
- a small ClusterRole: `get` on that one namespace (to read its PSA labels) and, optionally, `list` on nodes.

Without node access, provider detection falls back to asking for the provider, and the fit check below is skipped with a note. `init` itself, and creating a namespace, need rights to create namespaces, Roles, ResourceQuotas, LimitRanges, NetworkPolicies and ServiceAccounts: on a shared cluster, a cluster admin, once. The preflight checks that the `ostia-test-runner` ServiceAccount exists.

#### 4.4 Node profiles

A profile names a node type and maps it per provider:

```toml
# ostia_dev/remote/profiles.toml (built in)
schema = 1

[profiles.l4]
kind = "gpu"                      # gpu | cpu | rdma
gpus = 1
compute_capability = "8.9"
cpu = "6"
memory = "24Gi"
ephemeral_storage = "60Gi"
[profiles.l4.gke]
node_selector = { "cloud.google.com/gke-accelerator" = "nvidia-l4" }
tolerations = [{ key = "nvidia.com/gpu", operator = "Exists", effect = "NoSchedule" }]
# GKE mounts its managed driver at /usr/local/nvidia, which the pixi image does not put on the paths
env = { PATH = "/usr/local/nvidia/bin:$PATH", LD_LIBRARY_PATH = "/usr/local/nvidia/lib64" }
[profiles.l4.eks]
node_selector = { "node.kubernetes.io/instance-type" = "g6.2xlarge" }   # or karpenter.k8s.aws/instance-gpu-name = "l4"

[profiles.cpu]
kind = "cpu"
cpu = "4"
memory = "16Gi"
ephemeral_storage = "20Gi"
```

Built-in profiles: `l4` (GKE and EKS; Azure has no generally available L4 size, so AKS users add their own GPU profile), `a100` and `h100` (GKE, EKS and AKS), and `cpu`. Users override fields or add profiles in their config (§1.2). `ostia-dev remote k8s profiles` shows the result after merging.

Prices for summaries are set per context, since they depend on the cluster's machine types and discounts:

```toml
[remote.k8s.contexts.gcp-us-central1-intuigence.prices]   # USD per hour, per profile
l4 = 0.85
cpu = 0.20
```

- **Resources:** `requests` equal `limits` for CPU, memory, `ephemeral-storage` and `nvidia.com/gpu`. The work volume is an `emptyDir` with `sizeLimit` equal to `ephemeral_storage`. The defaults (L4: 6 CPUs and 24 GiB, which fit GKE's `g2-standard-8` after system reservations; 60 GiB of storage for GPU suites, 20 GiB for CPU) are revisited after the first real runs report their usage.
- **Fit check:** `profiles --context` and the preflight compare the profile's requests with the allocatable resources of a node matching its selector. A profile that fits no node fails early with exit 2, naming the node shape found and the profile keys to lower.
- **Environment:** a profile may set per-provider `env` entries for the pod, like the GKE driver paths above.
- **Architecture:** a profile may set `arch = "arm64"` (Graviton, T2A, Grace), which adds `kubernetes.io/arch`. The image is multi-arch.

#### 4.5 Pod, image and supervisor

- **Image:** `ghcr.io/prefix-dev/pixi:<version>-noble@sha256:…` (Ubuntu 24.04, the tier-1 platform; amd64 and arm64), with the pixi version CI uses. Renovate bumps the digest. The CUDA toolkit comes from the locked pixi environment. The driver comes from the node: the NVIDIA container toolkit injects it on EKS, AKS and most self-managed clusters (`NVIDIA_DRIVER_CAPABILITIES=compute,utility`), while GKE mounts its managed driver at `/usr/local/nvidia`, which the GKE profiles add to `PATH` and `LD_LIBRARY_PATH`. The preflight checks that `nvidia-smi` and `libcuda.so.1` are found before anything else runs. git comes from pixi (§2.4), because the pod can't `apt-get`.
- **User:** uid 1000, `HOME=/w/home`, everything writable under the `/w` emptyDir.
- **Supervisor:** the container's command is a small POSIX `sh` script shipped in the pod spec (from `ostia_dev/remote/pod/`). It:
  1. waits for the code (`/w/.ostia/ready`), for at most 10 minutes (for two-pod runs, at least the schedule timeout, §4.11);
  2. runs the pipeline of §3.2, recording each step in `/w/.ostia/steps.json` and the full output in `/w/.ostia/log.txt`;
  3. waits for the CLI to copy the results: it exits with the test's code as soon as `/w/.ostia/collected` exists, or after a 10-minute collection window if the CLI never comes back.

  So a crashed CLI still ends the pod and frees the GPU by itself.
- **Streaming:** the CLI follows `kubectl logs -f --timestamps`. Long streams drop (API server and kubelet timeouts), so the CLI resumes with `--since-time` from the last line it saw and drops duplicates. The complete log is copied back anyway.
- **While waiting for a node,** the CLI shows one status line with elapsed time, built from the Job's and pod's events: `FailedScheduling`, `TriggeredScaleUp` / `NotTriggerScaleUp`, `Pulling` / `Pulled`, `ContainerCreating`. After `--schedule-timeout` (default 20 minutes) the run ends with exit 3, printing the last `FailedScheduling` reason and the profile key to change.

#### 4.6 Isolation

The runner runs reviewed code, but on clusters shared with other workloads. The defaults:

- **Pod Security `restricted`** on the namespace (`pod-security.kubernetes.io/enforce: restricted`). Pods run with `runAsNonRoot`, `allowPrivilegeEscalation: false`, `capabilities.drop: [ALL]`, `seccompProfile: RuntimeDefault`, and no `hostNetwork`, `hostPID` or hostPath.
- **No Kubernetes or cloud identity:** a dedicated ServiceAccount `ostia-test-runner` with no RBAC bindings, `automountServiceAccountToken: false` on the pod, and no Workload Identity, IRSA / EKS Pod Identity or Azure Workload Identity annotations.
- **ResourceQuota and LimitRange** from `init`: a cap on GPUs, CPU, memory and ephemeral storage in the namespace, and defaults for any pod that doesn't set them.
- **NetworkPolicy:** default-deny ingress and egress for the namespace. Egress allows DNS (UDP and TCP 53) to kube-dns only, and TCP 443 to `0.0.0.0/0` except a blocklist. The blocklist always holds `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `100.64.0.0/10` and `169.254.0.0/16`, which covers node, VPC and tailnet addresses and the cloud metadata servers. Clusters also use addresses outside those ranges (GKE's default Service range is `34.118.224.0/20`; some clusters use privately used public IPs), so `init` adds the cluster's pod and Service CIDRs where it can detect them, and any `blocked_cidrs` listed for the context in the user config. Everything a run needs (conda-forge, prefix.dev, ghcr.io, github.com for CPM) is public HTTPS. Two-pod runs get a per-run NetworkPolicy, owned by the Job, that allows traffic between pods with the same `ostia.dev/run-id`. The Kubernetes API server is reachable only if its endpoint is a public IP on 443; `verify` reports whether it is.
- **NodeLocal DNSCache:** on clusters that run it, pods resolve through `169.254.20.10`, which the blocklist would cut off. `init` detects it and allows UDP and TCP 53 to that address only. *(update: Rollout PR A: the cache runs on the node's host network, which a NetworkPolicy can't reliably select on every CNI: on GKE Dataplane V2 about a third of the lookups were dropped even with that address and the kube-dns Service IP allowed. So when `init` detects NodeLocal DNSCache, the DNS rule allows UDP and TCP 53 to any destination; without it, DNS stays limited to kube-dns. HTTPS keeps its blocklist, and the metadata server's API stays unreachable.)*
- **GPU requests** go through `nvidia.com/gpu`, never `privileged`.

These guarantees hold only if the cluster's network plugin enforces NetworkPolicy, and a plugin that doesn't ignores policies silently. `verify` checks that each one actually holds, from a probe pod with the run's exact spec: the metadata server is unreachable, there's no service-account token, the API server, the private ranges and an in-cluster ClusterIP Service on 443 are unreachable, and DNS and `https://github.com` work. `verify` is a manual check, documented as the first step on every new cluster or namespace. It is not run automatically, and runs don't require it.

#### 4.7 Cache (opt-in)

Every run downloads the locked environment and CPM sources into its own emptyDir, which is clean and isolated. `--cache` mounts a PVC, `ostia-test-cache` (created on first use, labelled `ostia.dev/managed=true`), for the rattler, CPM and ccache directories. The trade-offs:

- An RWO volume is zonal and serves one pod at a time: a run on a node in another zone can't schedule, and two cached runs don't run at once.
- A cache is written by whatever code ran last. Everyone who uses the namespace with `--cache` shares that trust boundary. Packages are still hash-checked by pixi against `pixi.lock`, and CPM sources are pinned by SHA.

The guide's "faster runs" section covers the other speed levers that don't change the default: `--cache`, keeping a warm node pool while iterating, running one suite or `-L` label instead of all, and `--no-build` when the command builds itself. The target is reruns of 5 to 10 minutes with `--cache` on a warm node; the first run of a cache-off suite may take 15 to 25 minutes.

#### 4.8 Teardown and garbage collection

- **Labels** on every object: `ostia.dev/managed=true`, `ostia.dev/run-id=<id>`, `ostia.dev/owner=<hash of user@host>`. Annotation `ostia.dev/expires=<UTC time>`.
- **Limits:** `activeDeadlineSeconds` counts from the Job's start, Pending time included, so it is the sum of every phase: `--schedule-timeout` + the code wait + `--timeout` + the collection window + the `--keep-on-failure` window when set + 15 minutes of margin (105 minutes with the defaults *(update: Rollout PR A: 115 minutes; 20 + 10 + 60 + 10 + 15, the original sum was an arithmetic slip)*; *(update: Rollout PR A3: a two-pod run's code wait is the larger of the code wait and the schedule timeout (§4.11), so its deadline is 125 minutes with the defaults)*). `ttlSecondsAfterFinished: 600`; a 10-minute wait for the code; a 10-minute collection window. The windows can be shortened by configuration for tests.
- **The CLI deletes** the run's Job (cascading to its pods and owned objects) on success, failure, Ctrl-C and SIGTERM, and then waits until the objects are gone. A second Ctrl-C skips the wait, not the delete, so the run is reported as "delete requested, not verified". If deletion can't be verified, the `cleanup` command is printed (exit codes in §3.5).
- **Every run starts** by deleting the owner's expired objects in the namespace, including Jobs still suspended past their `ostia.dev/expires` time. A CLI that dies between creating the suspended Job and unsuspending it leaves one: no deadline or TTL runs while a Job is suspended. It costs nothing, since no pod exists, and `cleanup` lists and deletes it.
- **`cleanup`** deletes the caller's runs; `--run-id` targets one run; `--all` covers every managed run in the namespace, after a confirmation. PVCs from `--cache` are deleted only by `cleanup --cache`.
- **A crashed CLI** leaves a run that still ends by itself: the supervisor exits after its windows, `activeDeadlineSeconds` kills a pod that hangs, `ttlSecondsAfterFinished` deletes the finished Job, and the ownerReferences delete the Service and the NetworkPolicy with it.
- **`--keep-on-failure[=N]`** (default 30 minutes, opt-in) keeps a failed pod for live debugging. The CLI copies the results but doesn't write the collected marker, prints the exact `kubectl exec` command and the time the pod will end, and extends the deadline by N. Ctrl-C, `cleanup --run-id` or `touch /w/.ostia/collected` ends it early.

```mermaid
stateDiagram-v2
    direction LR
    [*] --> Preflight
    Preflight --> [*]: config or guardrail error (exit 2, nothing created)
    Preflight --> Created: Job (suspended), owned objects, unsuspend
    Created --> Scheduling
    Scheduling --> Running: pod started, code uploaded
    Scheduling --> TearingDown: schedule timeout or image error (exit 3)
    Running --> Collecting: command finished
    Running --> TearingDown: deadline, eviction, lost connection (exit 3), Ctrl-C (130)
    Collecting --> Debugging: failed and --keep-on-failure
    Debugging --> TearingDown: window ends, Ctrl-C or cleanup
    Collecting --> TearingDown
    TearingDown --> Verified: every object gone
    TearingDown --> Unverified: delete not confirmed (exit 4)
    Verified --> [*]
    Unverified --> [*]: cleanup command printed
```

#### 4.9 GPU preflight and CUDA architecture

- `tools/ci/gpu_preflight.sh` runs first on GPU profiles: it prints the fingerprint (`nvidia-smi` name, driver, compute capability, memory; `uname`) and fails on a driver older than R580 (RFC-0001 §1.1). On GKE the fix text names the node pool's `gpu-driver-version=latest` setting, since GKE's default driver may be older.
- The profile's `compute_capability` must match `nvidia-smi --query-gpu=compute_cap`. A mismatch means the run landed on the wrong node type, and the preflight fails with both values.
- **Architecture:** the default `dev` preset picks `native` when a GPU is detected (RFC-0001 §1.1), and the runner checks that `ostia-summary.txt` says so. Other presets get `-DCMAKE_CUDA_ARCHITECTURES=<cc>-real` from the profile (for example `89-real` on an L4), so the `level-*` presets build only what the node runs. The fixed `gpu-ci` preset (architecture 89) is removed in PR B.

#### 4.10 Credentials and what reaches the pod

- kubeconfig, tokens and cloud credentials never leave the Mac; kubectl uses them locally.
- No environment variables are forwarded by default. `--env-var KEY=VALUE` passes one, and its key (never its value) is recorded in `summary.json`. Keys matching `*TOKEN*`, `*SECRET*`, `*KEY*` or `*PASSWORD*` are refused unless `--allow-secret` is given; allowed secret values go into a per-run Secret owned by the Job and reach the pod as environment variables from it.
- **Upload:** the CLI builds the tarball on the Mac in `prepare`, before anything exists in the cluster. It holds `git ls-files -co --exclude-standard` minus `git ls-files -d`: tracked and untracked files, without tracked files deleted locally and never git-ignored ones such as `.env` or `build/`. That's the set `check_cuda.py` copies. The CLI warns about untracked files over 10 MB and refuses a tarball over 500 MB. The tree hash and size go into `summary.json`. *Update (Rollout PR A): `.env` was not git-ignored in this repository, so PR A adds `.env` and `.env.*` to `.gitignore`, and the tarball also skips untracked files that look like secrets whatever the ignore rules say (`.env`, `.env.*`, `*.pem`, `*.key`, `*.p12`, `id_rsa*`, `id_ed25519*`, `.netrc`, `.pypirc`, `.npmrc`, `*kubeconfig*`, `.kube/**`), with a warning listing them. Tracked files are uploaded as before.*
- **`--ref <sha>`** runs a pushed commit instead of the working tree, and **`--ref pr/<n>`** runs a pull request's head. The CLI fetches it on the Mac (`git fetch origin <sha>` or `pull/<n>/head`) and uploads `git archive <sha>` through the same path, so the pod never clones and needs no git before pixi.

**Contributor code.** A maintainer may run a contributor's pull request (ADR-0014 rule 2), but only after reviewing the diff at that SHA; the runner is not a sandbox for unreviewed code. A `--ref pr/<n>` run is treated as contributor code, and the CLI also:

- refuses `--cache`, so a shared cache can't be poisoned, and refuses `--env-var`;
- records `code: contributor` and the pull request number in `summary.json` and in the summary line.

#### 4.11 Two-pod runs

For the UCX programs that need two nodes (`rdma_put`, `gdr_stream`, `tcp_put`, `dual_link --mode rails`; RFC-0001 §6.4):

- `--pods 2` creates an Indexed Job with required pod anti-affinity, so the two pods land on different nodes (`--same-node` relaxes it).
- The pods may start minutes apart, for example when one waits for a scale-up. The CLI uploads the code only once every pod is Running, and each supervisor's code wait lasts at least the schedule timeout. Each pod gets `OSTIA_RANK` (0 or 1), `OSTIA_SIZE=2`, `OSTIA_PEER_HOST=<job>-0.<service>` and `OSTIA_PORT`. The same command runs in both. *(update: Rollout PR A3: `OSTIA_PORT` is 29400; `OSTIA_RANK` comes from the Indexed Job's completion-index annotation through the downward API; the headless Service has the Job's name and `publishNotReadyAddresses: true`; the per-run NetworkPolicy allows every port between the run's pods, because UCX opens more connections after the rendezvous; it is created only when the namespace has the guardrail policies, since in an unguarded one it would be the pods' only egress rule. The CLI polls both pods' logs every 3 seconds (`kubectl logs -l <run> --prefix --tail=-1`) instead of following them, so a pod that dies is seen while the other still runs.)*
- The programs already take `--listen PORT` and `--connect HOST:PORT` (#19). Mapping the ranks to them in the benchmark driver (rank 0 to `--listen $OSTIA_PORT`, rank 1 to `--connect $OSTIA_PEER_HOST:$OSTIA_PORT`) is new work in PR A; today the driver only uses the single-node launcher. Rank 1 retries the peer's DNS name and the connection for up to 2 minutes, because the headless Service's record appears only once pod 0 is Ready. *(update: Rollout PR A3: the driver (`ostia_bench.py run --remote`) retries the name for 2 minutes and leaves the connection retry to the program, since a test connection would take rank 0's single accept; before each program the two drivers meet on `OSTIA_PORT` + 1 (waiting up to 30 minutes), because each pod builds on its own and the programs wait only 60 seconds for a peer; only rank 1, the source, writes the record and the evidence.)*
- Results are collected from each pod into `rank-<i>/`. If either pod fails, the run fails and both are torn down. *(update: Rollout PR A3: `--pods` takes 1 or 2.)*

#### 4.12 RDMA profiles (opt-in, privileged)

Two-node RDMA and GPUDirect need things `restricted` forbids: RDMA device resources (`rdma/*` from the NVIDIA network operator's shared-device plugin, or SR-IOV with Multus), a raised memlock limit and the `IPC_LOCK` capability. Profiles of `kind = "rdma"`:

- run only in a namespace labelled PSA `privileged`, which `init --privileged` creates. A normal run never creates or relabels one;
- request the profile's `rdma/*` resources and add `IPC_LOCK`. Kubernetes doesn't set ambient capabilities, so an added capability has no effect for a non-root process: RDMA pods run as root inside their privileged namespace. The memlock limit can't be set in a pod spec; it comes from the container runtime (`LimitMEMLOCK`), and `verify` checks it with `ulimit -l`;
- use the pod network by default, through the device plugin, so the service-account, token, cloud-identity and NetworkPolicy defaults of §4.6 still apply;
- may set `host_network = true` only as a separate, explicit profile field *(update: Rollout PR A3: the profile fields are `rdma_resources = { "rdma/<name>" = "<n>" }` and `host_network`; either on a profile not of kind `rdma` is an exit 2)*. A host-network pod reaches the node's metadata server, so it gets the node's cloud identity (the GKE node service account, the EKS node IAM role), and NetworkPolicies don't apply to it. The summary states this for every such run, and `verify` probes the metadata server in that mode;
- are refused in a `restricted` namespace, with the fix naming `init --privileged`.

No RDMA-capable cluster is available today, so this section is designed but unproven. Its first real use confirms it (Open questions).

### 5. The `container` backend

`ostia-dev remote container` runs the same pipeline (§3.2) in a local container with podman or docker, whichever is found first; if neither is, it fails with the install fix, as `check_cuda.py` does today.

- The same pinned image and supervisor as §4.5. The CLI builds the same tarball on the host (§4.10) and streams it into the container on stdin, so the pipeline is identical and the container needs no git before pixi.
- On a Mac the container is `linux/arm64`, which runs natively on Apple silicon. With no GPU, `--no-test` makes it compile-only, which is how it replaces `check-cuda`: `ostia-dev remote container --env cuda-12 --preset release --no-test`.
- On a Linux host with an NVIDIA GPU and the NVIDIA container toolkit, `--gpus` passes the GPU through and GPU suites run.
- `--env` may repeat; the pipeline runs once per environment, as `check-cuda` built both `cuda-12` and `cuda-13` by default.
- A named volume caches downloads, as `check_cuda.py`'s `ostia-pixi-cache` does. That cache is on the developer's own machine.
- CPU CI uses this backend to test the pipeline itself without a cluster (Testing).

### 6. Gate runs on Kubernetes, and the overlap with RFC-0004

The runner takes over everything the dropped GPU CI did, and lets a cluster with the right hardware serve as a gate machine.

| Work | Before | Now |
| --- | --- | --- |
| GPU unit, integration and multi-process tests at four telemetry levels | GPU CI (RFC-0001 §4.2) | `--suite gpu` on an L4 profile |
| `compute-sanitizer`, nightly | GPU CI | `--suite sanitizer` |
| Benchmark driver end to end, `--smoke` of every gate program | GPU CI (#18, #19) | `--suite bench-smoke` |
| Overhead self-test and the A/A noise floor on the L4 | GPU CI (RFC-0001 §6.6) | `--suite overhead-aa`; the quiet-box fallback stays with RFC-0004 |
| M0 gate runs (nvlink-node, rdma-pair, tcp-efa-pair) | RFC-0004 rented setups | RFC-0004, **or** a k8s machine declared in the setup file (below) |
| Topology captures (RFC-0003) | RFC-0004 | the same, on whichever machine runs the gate |

**Declaring a k8s machine for a gate setup.** A setup file in `infra/setups/<setup>.yaml` (RFC-0004 §1, #19) may name a machine with `backend: k8s`, as its primary or as a pre-declared fallback:

```yaml
machines:
  fallback:
    backend: k8s
    machine: nvlink-a100x4   # a logical name; the user config says where it is
    pods: 1
    capabilities: [nvlink-p2p, cuda-ipc]
```

The repository never names a cluster (§1.2), so the user config maps the logical name:

```toml
[remote.k8s.machines.nvlink-a100x4]
context = "gcp-us-central1-intuigence"
namespace = "ostia-gate"
profile = "a100x4"           # a user profile with 4 GPUs on one node
```

A setup file whose k8s machine has no mapping is exit 2, naming the key to add. *(update: Rollout PR A3: a k8s machine also declares `accelerators` (`"<GPU>:<n>"`, as rented machines do), and the mapped profile must have at least that many GPUs; `pods` stands in for `nodes`; a two-pod `dual_link` takes its two NIC ports from the rdma profile's `rdma_nics`. `remote gate` is partial in PR A3: it runs the capability check, the mapping, the RDMA rule, the evidence-counter probe, the gate workloads with `--evidence` and the evidence check (`--baseline` adds `compare.py`); RFC-0004 §1.1's active probes, RFC-0003's captures and `rent`'s fallback handover come with RFC-0004 PR 7 and RFC-0003 PR 6.)*

`ostia-dev remote gate <setup>` runs it. A k8s machine passes exactly the checks a rented one does:

- RFC-0004 §1.1's preflight and active capability probes;
- RFC-0001 §6.4's transport evidence;
- RFC-0003's manifest-gated captures. *(update: Rollout PR 6b: captures are now checked: a gate whose setup lists `topo_capture` in `also_run` fetches them, writes `pair.json` for two pods and stamps the records with the pair id only when both captures are complete. A rejected or partial capture leaves the records unstamped, and a gate compared with a baseline that has a pair id exits 1 (#45).)*

Before any gate workload runs, a probe checks that every evidence counter it needs is readable in the pod (InfiniBand port counters in sysfs; `nvidia-smi nvlink` counters). If one isn't, the gate fails closed on that machine. RDMA gate workloads (`rdma_put`, `gdr_stream`, `dual_link` rails) must use an `rdma` profile (§4.12): loading a setup file that puts them on another kind of profile is an exit 2, because the counters that prove RDMA traffic are only visible with RDMA devices in the pod. NVLink workloads may use normal GPU profiles.

Spend on k8s machines has no rent ledger: runs on the maintainer's own clusters are outside the M0 budget (ADR-0014), and each summary records node-hours times the context's price for the profile (§4.4).

### 7. Changes to accepted RFCs

This RFC changes interfaces that earlier accepted RFCs define. Each change lands with the PR that implements it:

- **RFC-0001:** the tool names of §3.1 and the quick-start wording of its first goal (§2.2 here); the `gpu-ci` preset (§4.9 here); GPU CI itself (ADR-0014).
- **RFC-0003:** `pixi run topo-show` and the capture tool become `ostia-dev topo show|capture` (§2.2).
- **RFC-0004:** `pixi run rent` and `tools/rent/` become `ostia-dev rent` inside the package (§2.2). The setup-file schema gains the `backend: k8s` machine type (§6). For a k8s machine, the price quote, the cap and the ledger reservation don't apply (there is no provider bill to guard); the rest of the lifecycle does, including trying the fallback when a gate capability is missing and refusing a second active run of the same setup. `ostia-dev remote gate <setup>` drives a setup whose chosen machine is a k8s one; `ostia-dev rent` drives SkyPilot and Azure machines, and hands over to `remote gate` when it falls back to a k8s machine.

## Failure handling

Every message follows the contract of §1.3. The run's state is always one of the rows of §3.5.

| Failure | Detected by | What the developer sees | Exit | State left |
| --- | --- | --- | --- | --- |
| No context, or no namespace and no TTY | Argument parsing | The flag or config key to set | 2 | Nothing created |
| Unknown context in the config, no TTY | Preflight | The detected provider and the TOML line to add | 2 | Nothing created |
| Namespace missing, no TTY and no `--yes` | Preflight | `init` or `--yes` | 2 | Nothing created |
| Guardrails missing | Preflight | Which one; `init` or `--allow-unguarded` | 2 | Nothing created |
| RDMA profile in a restricted namespace | Preflight | `init --privileged` | 2 | Nothing created |
| RDMA gate workload on a non-RDMA profile | Setup-file load | The workload, the profile, the rule | 2 | Nothing created |
| kubectl more than one minor from the server | Preflight | Warning with both versions and the fix | — | — |
| Tarball over 500 MB | Upload | The largest untracked files | 2 | Nothing created |
| Secret-looking `--env-var` | Argument parsing | The key and `--allow-secret` | 2 | Nothing created |
| kubectl `Forbidden` (RBAC) | kubectl | The verb and resource, and the Role that grants it (§4.3) | 2 | Nothing created, or deleted |
| Credential plugin missing or expired | kubectl | The plugin and the provider's login command | 2 | Nothing created |
| ServiceAccount `ostia-test-runner` missing | Preflight | `init` | 2 | Nothing created |
| Profile fits no node in the pool | Preflight (fit check) | The node shape and the profile keys to lower | 2 | Nothing created |
| Quota exceeded, PSA or webhook rejection | Job `FailedCreate` event | The admission message | 2 | Deleted |
| Pod never schedules (no capacity, taints, scale-up refused) | Events, `--schedule-timeout` | The last `FailedScheduling` reason and the profile key | 3 | Deleted |
| `--cache` volume in another zone, or in use | Events | The volume, its zone, and `cleanup --cache` or a run without `--cache` | 3 | Deleted |
| Code never arrives (upload failed, CLI died) | Supervisor code-wait | The pod exits after the wait | 3 | Ends by itself |
| `nvidia-smi` or `libcuda.so.1` not found | Preflight | The paths searched; the profile's `env` (§4.4) | 3 | Deleted |
| `pixi install` fails (egress blocked, DNS) | Pipeline | pixi's error; `verify` | 3 | Deleted |
| Build fails | Pipeline | The compiler output | 1 | Deleted |
| GPU tests skipped or not run on a GPU profile | Pipeline guard | The skipped tests | 3 | Deleted |
| Out-of-memory kill | Pod status | Memory, and the profile keys | 3 | Deleted |
| Results over the cap, or a symlink | Collect | A warning listing the dropped items | the run's code | Deleted |
| `--ref pr/<n>` with `--cache` or `--env-var` | Argument parsing | The rule (§4.10) | 2 | Nothing created |
| Image pull fails | Events | The image and the pull error | 3 | Deleted |
| Driver older than R580 | `gpu_preflight.sh` | The fingerprint and the rule | 3 | Deleted |
| Compute capability differs from the profile | Preflight | Both values; the profile's node selector | 3 | Deleted |
| Evidence counters unreadable (gate) | Evidence probe | Which counters | 1 (gate fails) | Deleted |
| ctest found no tests | Pipeline guard | The build directory and the ctest arguments | 1 | Deleted |
| Tests fail | Command | ctest's output; results copied | 1 | Deleted |
| Eviction (ephemeral storage) | Pod status | `ephemeral storage` and the profile key to raise | 3 | Deleted |
| Node preempted (spot) | Pod status | Preempted; the rerun command | 3 | Deleted |
| Deadline reached | Job status | The step that was running | 3 | Deleted |
| Log stream drops | kubectl | Nothing; the stream resumes | — | — |
| Connection lost for good | kubectl | The run ID and `cleanup --run-id` | 3 | Ends by itself (§4.8) |
| Ctrl-C | Signal | Teardown progress | 130 | Deleted |
| CLI killed (SIGKILL, laptop asleep) | Not detectable | Next run's GC reports what it removed | — | Ends by itself (§4.8) |
| Delete not confirmed | Teardown | The objects left and the `cleanup` command | 4 if the tests passed, else the run's code | May remain |

## Observability

- `summary.json` and the summary line (§3.4) record, per run: the identity of what ran (SHA, tree hash, upload size), where (context, namespace, profile, node, GPU fingerprint), per-step exit codes and durations, and node-hours with a cost estimate.
- `ostia-dev remote k8s usage` totals node-hours and estimated cost from the local run records.
- `-v` prints every kubectl call. The status line shows scheduling and pull progress from events (§4.5).
- Nothing is sent anywhere; there is no telemetry.

## Performance

- The CLI's own overhead (preflight, upload of a typical tree of a few MB, Job creation) targets under 30 seconds, excluding node scale-up.
- Reruns target 5 to 10 minutes with `--cache` on a warm node; a cache-off first run of `--suite gpu` may take 15 to 25 minutes (estimates, measured by the first runs' `summary.json`).
- Defaults favour isolation over speed (the cache is off); §4.7 lists the levers.

## Testing

| Test | Runs on |
| --- | --- |
| Framework: config merge and schema rejection, error contract, exit codes, TTY and non-TTY prompts, `--yes` | CPU CI (unit) |
| Manifests: Job, Indexed Job, Service, NetworkPolicy, ResourceQuota, LimitRange, securityContext, per provider and profile, as golden YAML | CPU CI (unit) |
| k8s backend with a fake `Kube` wrapper that records kubectl calls and replays scripted outputs: suspend, owned objects, unsuspend; teardown on success, failure, exception, Ctrl-C and lost connection; GC of expired runs; `cleanup` scopes; log-stream resume without duplicates; the collected marker and the window; `--keep-on-failure`; event-based status and the schedule timeout; eviction mapped to exit 3; skew warning; context and namespace prompts and config writes | CPU CI (unit) |
| Pipeline guards: zero-test ctest fails; per-step codes; results allowlist, symlink rejection, size cap | CPU CI (unit) |
| Setup-file validation for gate runs, including the RDMA profile rule | CPU CI (unit) |
| `container` backend: the real pipeline on a CPU environment | CPU CI (Linux, docker) |
| `kind` cluster (pinned, v0.24 or newer, whose default CNI enforces NetworkPolicy; PR A confirms it), with shortened windows: a CPU run end to end, a two-pod run, SIGKILL of the CLI followed by TTL cleanup, and the policies: default-deny, the private-range block and the intra-run allow | CPU CI, on pull requests that touch `tools/ostia-dev/` and nightly |
| PR B parity: each old entry point and its new subcommand give the same exit code, output and files | CPU CI, PR B only |
| Isolation on a real cluster | Manual: `ostia-dev remote k8s verify` |
| RDMA profile | Manual, on the first RDMA-capable cluster |

Two gaps remain. Isolation on each real cluster depends on its CNI, so `verify` is the check there; kind only proves the policies are right. And the RDMA profile has no cluster to test on yet.

## Alternatives considered

- **Automated GPU CI on Cirun (RFC-0001 §4.2).** Built for untrusted fork code, with label gating, approval environments, a separate AWS account and a budget action. The maintainer dropped it (ADR-0014): it guards against a threat on-demand maintainer runs don't have, it covers one GPU type, and it can't test uncommitted work.
- **ARC runners on a Kubernetes cluster.** The interim plan: GitHub Actions runner scale sets on a tainted spot L4 pool of the maintainer's GKE cluster. Still automated CI, so it still needs every fork-safety measure, plus a GitHub App and budget enforcement on the cluster. It was dropped before anything was created.
- **Plain `kubectl run` scripts.** Short to write, but no teardown when the script dies, no isolation defaults, no results contract, and every provider's selectors hard-coded. The Job's deadline, TTL and ownerReferences give the crash guarantees that a script can't.
- **Bare pods instead of Jobs.** Simpler, but a finished pod stays until someone deletes it, and two-pod runs need hand-made hostnames. A Job adds `ttlSecondsAfterFinished`, `backoffLimit: 0` and Indexed completions with stable DNS names.
- **SkyPilot's Kubernetes backend.** SkyPilot is already approved for RFC-0004 and launches tasks on any cluster by context. But its pods need a ServiceAccount with RBAC, run as root with sshd, and don't pass PSA `restricted`; and its lifecycle doesn't give this RFC's results layout, exit codes or crash-proof teardown. It stays the tool for RFC-0004's clouds.
- **The official Python Kubernetes client, or lightkube.** Typed and easy to mock, but the official client's streaming of binary stdin over exec has historically been fragile, and lightkube has no exec at all, so kubectl would be needed anyway. kubectl also reuses the developer's credential plugins with no extra code.
- **A per-run namespace.** Deleting the namespace cleans up everything, but every developer then needs cluster-scoped rights to create namespaces, and the guardrails would be created each time by the identity they're meant to limit.
- **An Ostia-built image with the environment preinstalled.** Much faster starts, but a build and publish pipeline, and an image that drifts from `pixi.lock` unless rebuilt on every lock change. Deferred; `--cache` is the opt-in speed lever.
- **argparse or click instead of typer.** argparse is what the existing scripts use and adds nothing, but nested groups, help and completion are all hand-written. click is typer's base. typer gives typed commands with the least code.
- **Moving every tool in PR A.** One big move would conflict with the open #17–#19; PR B moves them after those merge.
- **Keeping the old pixi task names as aliases.** Rejected by the maintainer in favour of one vocabulary and a clean switch, with a CHANGELOG table and parity tests (§2.3).

## Rollout

| PR | Content | Implements |
| --- | --- | --- |
| A | `tools/ostia-dev/` package, framework, `remote k8s` and `remote container`, suites, `docs/guides/remote-runs.md` and its row in `docs/guides/README.md`, the kind CI job, pixi and Renovate entries for typer, kubectl, git and the image digest | §1, §3–§6 |
| — | Rework of #17–#19 per ADR-0014 (keep the GPU tests, drop the workflows) | ADR-0014 |
| B | Migration and hard switch: existing tools move in, old names removed, CI, pre-commit and docs updated, CHANGELOG table, parity tests; `gpu-ci` preset removed | §2 |
| later | `ssh` backend; `rent` as a backend (RFC-0004) | §3.1 |

- This RFC must be Accepted before PR A starts. ADR-0014 merges after it.
- The guide shipped with PR A (RFC-0001 Rollout: each PR ships its guide) covers cluster setup (`init`, `verify`), profiles and suites, faster runs (§4.7), debugging (`--keep-on-failure`, `-v`) and, for contributors without a cluster, the `container` backend and how to ask a maintainer for a GPU run on their pull request (`--ref <sha>`).

## Open questions

- **`ssh` backend:** worth it for a workstation with a GPU, or does the `container` backend on that machine cover it?
- **`rent` as a backend:** RFC-0004's `rent` has its own lifecycle, ledger and spend limits; how much of §3's pipeline it can reuse is decided when PR 7 lands.
- **RDMA on a real cluster:** the privileged profile (§4.12) and the evidence probe (§6) are unproven until an RDMA-capable cluster runs them.
- **Cache trust:** whether a shared `--cache` PVC should be per developer (keyed by `ostia.dev/owner`) rather than per namespace.
- **typer-slim:** measured in PR A (§2.4). *Update: answered; it is a shim over typer, so PR A uses typer.*
