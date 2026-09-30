# Remote runs

How to run Ostia's build and tests somewhere other than your own machine with `ostia-dev remote`: in a local container today, and on a Kubernetes cluster's GPU or CPU nodes once the `k8s` backend lands. The design is [RFC-0005](../rfcs/0005-dev-cli-remote-runner.md); why GPU testing works this way is [ADR-0014](../adr/0014-on-demand-remote-test-runs.md).

## Overview

A remote run takes your working tree as it is, uncommitted changes included, and runs one pipeline on it:

1. **Upload.** The CLI builds a tarball on your machine from `git ls-files`: tracked and untracked files, without git-ignored ones and without files you deleted. Untracked files that look like secrets (`.env*`, `*.pem`, `*.key`, SSH keys, `.netrc`, kubeconfigs, …) are skipped with a warning. A tarball over 500 MB is refused.
2. **Preflight** (GPU profiles only): `tools/ci/gpu_preflight.sh`, the compute-capability check and `libcuda.so.1`.
3. **Install:** `pixi install --locked -e <env>`. A `pixi.lock` that is out of date with `pixi.toml` is refused on your machine first, before anything starts.
4. **Configure and build** the preset (`--no-build` skips it).
5. **Command:** by default `ctest --output-on-failure`, in the preset's build directory; `-- <command>` runs something else, and `--suite <name>` runs a named suite instead.

Every command runs as `pixi run ostia-dev …`, or as plain `ostia-dev …` inside `pixi shell`. `ostia-dev remote container --help` lists every flag; `-v` prints each external command it runs.

### Suites

| Suite | Runs |
| --- | --- |
| `cpu` | build and `ctest -L cpu` |
| `gpu` | every test at telemetry levels `off`, `metrics`, `trace` and `debug`, plus the check that the `dev` preset picked `native` |
| `sanitizer` | `compute-sanitizer` memcheck, racecheck and synccheck on the GPU tests, and memcheck on the CUDA bench programs |
| `bench-smoke` | the benchmark driver end to end, and every `fabric/bench` program with `--smoke` |
| `overhead-aa` | the overhead self-test (fails on a wrong verdict) and the A/A noise floor, which is reported and never fails the run |

The GPU suites need a GPU: a Linux host with `--gpus`, or a cluster's GPU node.

## The container backend

`ostia-dev remote container` runs the pipeline in a local podman or docker container, whichever it finds first (`--engine` picks one). It uses the same pinned image and in-container supervisor as cluster runs, so a pipeline that works here works there. It needs [podman](https://podman.io) (`brew install podman && podman machine init && podman machine start`) or Docker.

**On a Mac** the container is `linux/arm64`, which runs natively on Apple silicon. There is no GPU, so CUDA checks are compile-only:

```bash
pixi run ostia-dev remote container --env default --suite cpu                                  # the CPU tests, on Linux
pixi run ostia-dev remote container --env cuda-12 --env cuda-13 --preset release --no-test     # compile the CUDA code with both toolkits
```

The second command does what `pixi run check-cuda` does; `--env` repeats, and each environment is its own run with its own result.

**On a Linux host with an NVIDIA GPU** and the NVIDIA container toolkit, `--gpus` passes the GPUs through. Name a GPU profile with it, so the run knows the GPU's compute capability and runs the GPU checks:

```bash
pixi run ostia-dev remote container --gpus --profile l4 --suite gpu
```

The container runs as uid 1000 with every capability dropped. Downloads are cached in the named volume `ostia-pixi-cache`, on your own machine, so a second run installs in seconds. The container is removed when the run ends, even after a failure or Ctrl-C; a later run removes any container a crashed CLI left behind.

## Results and exit codes

Each run's results land in `build/remote/<run-id>/`:

```text
build/remote/container-cpu-20261002-141501-a1b2c3/
  summary.json        # what ran and where: SHA, tree hash, env, steps with exit codes and durations, exit code
  log.txt             # the full output
  fingerprint.txt     # gpu_preflight.sh output (GPU runs)
  ostia-summary.txt   # the configure summary
  junit.xml           # ctest's results; junit-<level>.xml for the gpu suite
  Testing/            # ctest's own output
```

Benchmark output is also copied to `bench/results/<run-id>/`, where `compare.py` looks for it. At the end the CLI prints one summary line, which is what you paste into a pull request:

```text
container-cpu-20261002-141501-a1b2c3  passed  cpu  sha 3f2a9c1+dirty(tree 9ab3…)  suite cpu  41s
  start 1s · upload 1s · install 17s · build 15s · test 2s
```

| Exit code | Meaning |
| --- | --- |
| 0 | Every step passed |
| 1 | The tests or the command failed, the build failed, or ctest found no tests |
| 2 | Usage or configuration error; nothing was started |
| 3 | Infrastructure failure: the result is unknown (the upload failed, preflight or install failed, out of memory, timeout, a GPU test skipped on a GPU profile) |
| 4 | The tests passed, but removing the container or pod could not be verified; the command to remove it is printed |
| 130 | Interrupted by Ctrl-C; everything was removed |

A failing `pixi install` is a 3, not a 1: usually the network, not your change. Rerun it.

## No GPU or cluster? Ask a maintainer

A pull request that touches GPU code needs a GPU run before it merges ([ADR-0014](../adr/0014-on-demand-remote-test-runs.md) rule 2 lists what counts). You don't need a cluster for that. Check the CUDA code compiles with the container backend (above), open the pull request, and ask in a comment. A maintainer reviews the diff at its head SHA, runs the suites on a GPU node with `--ref pr/<n>` (or on a checkout of that SHA), and pastes the summary line into the pull request. A `--ref pr/<n>` run is recorded as contributor code, and it may not use `--cache` or `--env-var`.
