# Remote runs

How to run Ostia's build and tests somewhere other than your own machine with `ostia-dev remote`: in a local container, or in temporary pods on a Kubernetes cluster's GPU or CPU nodes. The design is [RFC-0005](../rfcs/0005-dev-cli-remote-runner.md); why GPU testing works this way is [ADR-0014](../adr/0014-on-demand-remote-test-runs.md).

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
| `topo-capture` | `ostia-dev topo capture` on the node; the runner fetches the capture ([below](#topology-captures)). A failed capture fails this suite |

The GPU suites need a GPU: a Linux host with `--gpus`, or a cluster's GPU node.

## The container backend

`ostia-dev remote container` runs the pipeline in a local podman or docker container, whichever it finds first (`--engine` picks one). It uses the same pinned image and in-container supervisor as cluster runs, so a pipeline that works here works there. It needs [podman](https://podman.io) (`brew install podman && podman machine init && podman machine start`) or Docker.

**On a Mac** the container is `linux/arm64`, which runs natively on Apple silicon. There is no GPU, so CUDA checks are compile-only:

```bash
pixi run ostia-dev remote container --env default --suite cpu                                  # the CPU tests, on Linux
pixi run ostia-dev remote container --env cuda-12 --env cuda-13 --suite cuda-compile             # compile the CUDA code with both toolkits
```

The second command compiles everything with nvcc, the benchmarks included, and runs no tests; it replaces the old `check-cuda` task. `--env` repeats, and each environment is its own run with its own result.

**On a Linux host with an NVIDIA GPU** and the NVIDIA container toolkit, `--gpus` passes the GPUs through. Name a GPU profile with it, so the run knows the GPU's compute capability and runs the GPU checks:

```bash
pixi run ostia-dev remote container --gpus --profile l4 --suite gpu
```

The container runs as uid 1000 with every capability dropped. Downloads are cached in the named volume `ostia-pixi-cache`, on your own machine, so a second run installs in seconds. The container is removed when the run ends, even after a failure or Ctrl-C; a later run removes any container a crashed CLI left behind.

## The k8s backend

`ostia-dev remote k8s` runs the same pipeline in a temporary pod on any Kubernetes cluster (GKE, EKS, AKS or another), picked by kube context and node profile:

```bash
pixi run ostia-dev remote k8s --context gke_my-project_us-central1_ostia --profile l4 --suite gpu
pixi run ostia-dev remote k8s --context <ctx> --profile l4 -- ctest -L gpu --output-on-failure
```

It uploads your working tree, streams the output, copies the results back and deletes the pod, also when the run fails or you press Ctrl-C. `--context` is always required: a run never lands on kubectl's current context by accident. ostia-dev downloads its own pinned kubectl (v1.36.5, checked against a sha256) on first use; `pixi run ostia-dev remote k8s kubectl` prints where it is.

### Setting up a cluster (once, by an admin)

1. A cluster admin creates the namespace and its guardrails: Pod Security `restricted`, a ResourceQuota and LimitRange, default-deny networking that lets pods reach only DNS and public HTTPS, and a ServiceAccount with no permissions:
   ```bash
   pixi run ostia-dev remote k8s init --context <ctx> --namespace ostia-test
   ```
   It prints the two `kubectl create …binding` commands that give each developer the Role `ostia-test-developer`. Run them for each developer.
2. **Check the isolation before the first run** on every new cluster or namespace. `verify` starts a probe pod with a run's exact spec and checks that the cloud metadata server, the API server, the node and private addresses are unreachable, that no service-account token is mounted, and that DNS and `https://github.com` work:
   ```bash
   pixi run ostia-dev remote k8s verify --context <ctx> --namespace ostia-test
   ```
   A failing check usually means the network plugin doesn't enforce NetworkPolicy; fix that before running anything.
3. Each developer's kubeconfig needs the provider's credential plugin: `gke-gcloud-auth-plugin` (`gcloud components install gke-gcloud-auth-plugin`) on GKE, the AWS CLI on EKS, `kubelogin` (`az aks install-cli`) on AKS. ostia-dev uses them unchanged; your credentials never leave your machine.

### Your first run on a cluster

On a terminal, the first run asks for the namespace and shows the provider it detected from the nodes, then offers to save both in `~/.config/ostia/config.toml`:

```toml
[remote.k8s.contexts."gke_my-project_us-central1_ostia"]
namespace = "ostia-test"
provider = "gke"
```

Without a terminal (in a script) it prints the lines to add and exits 2. Add `[remote.k8s.contexts."<ctx>".prices] l4 = 0.85` to get a cost estimate in each summary; `pixi run ostia-dev remote k8s usage` totals node-hours and cost from your local run records.

### Profiles

A profile names a node type and maps it per provider: `l4` (GKE, EKS), `a100` and `h100` (GKE, EKS, AKS) and `cpu`. `pixi run ostia-dev remote k8s profiles` lists them after your config's overrides; with `--context` it also checks each against the cluster's nodes. Override a field, or add your own profile, in the config:

```toml
[remote.k8s.profiles.l4.gke]
ephemeral_storage = "80Gi"
```

A profile gets exactly what it requests (requests equal limits). A pool that scales from zero is fine: the run waits for the autoscaler for up to `--schedule-timeout` (20 minutes), showing the scheduling events as it goes.

### Faster runs

A cache-off first run of `--suite gpu` downloads the whole environment and may take 15 to 25 minutes. To go faster:

- `--cache` mounts a volume (`ostia-test-cache`) for the pixi, CPM and ccache downloads, so reruns take 5 to 10 minutes. The volume is zonal and serves one run at a time, and everyone who uses it in the namespace shares it; `cleanup --cache` deletes it.
- Keep a warm node in the pool while you iterate.
- Run one suite or one `-L` label instead of everything, and `--no-build` when the command builds by itself.

### Debugging a run

- `-v` prints every kubectl command.
- `--keep-on-failure` (30 minutes, or `--keep-on-failure=10m`) keeps a failed pod: the CLI prints the `kubectl exec` command and when the pod ends, and waits. Ctrl-C, or `touch /w/.ostia/collected` in the pod, ends it early.
- If a run's teardown can't be confirmed (exit 4, or a second Ctrl-C), the CLI prints the command to remove what's left:
  ```bash
  pixi run ostia-dev remote k8s cleanup --context <ctx> --namespace ostia-test --run-id <run-id>
  ```
  A crashed CLI's run still ends by itself: the pod exits after its windows, the Job's deadline and TTL remove it, and your next run removes anything of yours that expired.

### Two-pod runs

The two-node benchmark programs (`rdma_put`, `gdr_stream`, `tcp_put`, `dual_link --mode rails`) need two pods on two nodes:

```bash
pixi run ostia-dev remote k8s --context <ctx> --profile l4 --pods 2 -- \
  ostia-dev bench run --remote --format ostia \
  --bench build/cuda-12/release/fabric/bench/ostia_fabric_bench_tcp_put
```

- Both pods run the same command. Each gets `OSTIA_RANK` (0 or 1), `OSTIA_SIZE=2`, `OSTIA_PEER_HOST` (rank 0's name) and `OSTIA_PORT` (29400). `ostia-dev bench run --remote` turns them into `--listen` on rank 0 and `--connect` on rank 1. Before each program the two drivers meet on port 29401 and start it together, waiting up to 30 minutes for the slower pod's build.
- The pods land on different nodes; `--same-node` lets them share one.
- In a guarded namespace, a per-run network policy lets the two pods reach each other on any port, and nothing else.
- The code is uploaded once both pods run, so one may wait for a scale-up. The output shows each line as `[rank 0]` or `[rank 1]`. If either pod fails, the run fails and both are removed.
- Results land in `rank-0/` and `rank-1/` inside the run's directory.

### Gate runs on a k8s machine

A setup file (`infra/setups/<setup>.yaml`) may declare a k8s machine, as its primary or its fallback:

```yaml
  fallback:
    backend: k8s
    machine: nvlink-a100x4     # a logical name
    pods: 1                    # 2 for the two-node setups
    accelerators: A100-80GB-SXM:4
    capabilities: [nvlink-p2p, cuda-ipc]
```

Your config says where that machine is:

```toml
[remote.k8s.machines.nvlink-a100x4]
context = "<ctx>"
namespace = "ostia-gate"
profile = "a100x4"             # a profile with at least the declared GPUs
```

`pixi run ostia-dev remote gate nvlink-node --fallback` then checks the capabilities and the mapping, probes that the evidence counters are readable, runs every gate workload through the benchmark driver with `--evidence`, and checks that each workload left records and evidence. `--baseline <file>` also compares the results with `compare.py --require-pass`. RDMA workloads need a profile of kind `rdma` (below).

When the setup's `also_run` lists `topo_capture`, the gate also captures the machine's topology. That step reports and never fails the gate ([Topology captures](#topology-captures)). In a two-pod gate, `pair.json` is built from the two captures and every record in `bench/results/<run>/results.jsonl` is stamped with the pair id (`provenance.topology_source` is `gate`); the copies in `build/remote/<run>/rank-*/` are not stamped. If either capture is rejected or partial, the records stay unstamped (`topology: null`). Against a baseline recorded with a pair id the gate then exits 1, because `compare` skips cases whose topology differs and a skipped case does not fail it.

The gate is partial for now: RFC-0004's active capability probes and `rent`'s fallback handover come with RFC-0004 PR 7. The command says so when it runs.

### RDMA profiles

Two-node RDMA needs RDMA devices, a raised memlock limit and `IPC_LOCK`, which the restricted namespace forbids. A profile of `kind = "rdma"` runs only in a namespace made with `init --privileged`:

```toml
[remote.k8s.profiles.ib]
kind = "rdma"
gpus = 1
compute_capability = "8.0"
cpu = "16"
memory = "64Gi"
ephemeral_storage = "100Gi"
rdma_resources = { "rdma/rdma_shared_device_a" = "1" }
rdma_nics = "mlx5_0:1,mlx5_1:1"   # the node's two NIC ports, for dual_link's rails in a gate
# host_network = true          # only if the cluster needs it; see below
```

Its pods run as root with `IPC_LOCK`, request the `rdma/*` resources, and use the pod network. `host_network = true` puts the pod on the node's network: it then has the node's cloud identity and no network policy applies, and the summary says so on every such run. No RDMA cluster has run this yet.

## Topology captures

A k8s run whose pod wrote `/w/capture/manifest.json` has its capture fetched before the pod is removed, whatever suite or command ran. The runner reads the manifest first, checks it, then fetches only the files it lists and verifies every hash; a rejected capture keeps only `diagnostics.txt`. The capture never changes the run's exit code. See [fixtures.md](fixtures.md) for what a capture is.

```text
build/remote/<run-id>/capture/            # one pod
  manifest.json  hwloc.xml  nvml.json  nics.json  meta.json  diagnostics.txt
  status.json                             # per node: accepted or rejected, the status, the reason
build/remote/<run-id>/capture/            # two pods
  node-0/  node-1/  pair.json  status.json
```

- `summary.json` has a `capture` entry with the same per-node result.
- A pod gets `NODE_NAME` (downward API), `OSTIA_CAPTURE_PROVIDER`, `OSTIA_CAPTURE_INSTANCE_TYPE` and `OSTIA_LEAK_IDENTIFIERS`. `topo capture` adds `NODE_NAME`, the kube context and the kubeconfig's cluster name to the leak check, as whole values and as long segments (region and zone names are skipped, [fixtures.md](fixtures.md#leak-check)).
- Exit codes of the capture tool: 0 complete, 2 partial, 1 failed, 3 leak or schema violation ([fixtures.md](fixtures.md#the-manifest-and-exit-codes)). `status.json` records `accepted` for 0 and 2 and `rejected` for the rest.
- Run `--suite topo-capture` to capture a node, with `--pods 2 --same-node` for two. The suite step is an ordinary command, so a failed capture fails that suite. In a gate, the capture is a report step instead.
- Without a pair id, a one-pod run's bench records carry the driver's own `topo1` id from `ostia-topo-capture --print-id`.

Captures are for you to inspect: do not commit one, use `ostia-dev topo import` after review.

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
  capture/            # a topology capture, when the pod wrote one (below)
```

Two-pod runs have the same files in `rank-0/` and `rank-1/`, with `summary.json` and `capture/` at the top. Benchmark output is also copied to `bench/results/<run-id>/`, where `compare.py` looks for it. At the end the CLI prints one summary line, which is what you paste into a pull request:

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

A pull request that touches GPU code needs a GPU run before it merges ([ADR-0014](../adr/0014-on-demand-remote-test-runs.md) rule 2 lists what counts). You don't need a cluster for that. Check the CUDA code compiles with the container backend (above), open the pull request, and ask in a comment. A maintainer reviews the diff at its head SHA, runs the suites on a GPU node, and pastes the summary line into the pull request:

```bash
pixi run ostia-dev remote k8s --context <ctx> --profile l4 --suite gpu --ref pr/<n>
pixi run ostia-dev remote k8s --context <ctx> --profile l4 --suite bench-smoke --ref pr/<n>   # when */bench/ changes
```

A `--ref pr/<n>` run is recorded as contributor code, and it may not use `--cache` or `--env-var`.
