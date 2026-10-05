# Topology fixtures

How to capture a machine's topology, check that the capture is safe to publish, and add it to the repository as a fixture. The design is [RFC-0003](../rfcs/0003-topology-fixtures.md). A fixture is a scrubbed description of a real machine that Ostia's discovery and planner tests replay on any laptop, without a GPU.

## Try it first

`ostia-topo-capture` is Linux-only: it reads sysfs, NVML and ibverbs. The repository holds a fake sysfs root (`fabric/tests/topology/data/capture-root/`, full of made-up identifiers) that the tool's tests use. To see the tool work without a GPU machine, run it in a Linux container:

```bash
pixi run ostia-dev remote container --env default --suite cpu     # the CPU tests on Linux, which include the tool's end-to-end test on the fake root
pixi run ostia-dev topo show fabric/tests/fixtures/topology/synthetic/nvswitch-8nic-2numa   # any platform
```

On a Linux box, `pixi run ostia-dev build` builds the tool, and `build/default/dev/fabric/tools/topo-capture/ostia-topo-capture --help` lists its flags. To run it on the committed fake machine, set what `fabric/tools/topo-capture/tests/e2e.py` sets: hwloc reads the made-up topology through `HWLOC_XMLFILE`, and the fake NVML is a test-built `libnvidia-ml.so.1` found through `LD_LIBRARY_PATH` and configured by `OSTIA_FAKE_NVML`. `--sysfs-root` and `--no-verbs` exist for this and for the tests only.

```bash
# Linux only; the tests build the fake libnvidia-ml.so.1 into the fake-nvml directory
D=fabric/tests/topology/data
HWLOC_XMLFILE=$D/hwloc-input.xml LD_LIBRARY_PATH=build/default/dev/fabric/tools/topo-capture/fake-nvml OSTIA_FAKE_NVML=$D/fake-nvml/complete.json \
  build/default/dev/fabric/tools/topo-capture/ostia-topo-capture --out /tmp/fake-capture \
  --provider test --instance-type test --sysfs-root $D/capture-root/sys --no-verbs
```

A machine without NVIDIA GPUs gives a complete capture with no `nvml.json`.

## Capture a machine

A capture needs the machine itself: bare metal or a VM you rented, with the NVIDIA driver loaded. Run it as a normal user, since root is not needed. A root run still works: it adds a note to `diagnostics.txt`, and the root-only DMI serial numbers it can read join the leak check's identifiers.

```bash
pixi run ostia-dev topo capture --out capture/ --provider onprem --instance-type dgx-h100 \
  --extra-identifiers site-names.txt
```

`topo capture` builds the tool if needed (it looks in `$OSTIA_TOPO_CAPTURE`, then `--build-dir`, then this checkout's dev build, then `PATH`), runs it and prints one line on what its exit code means. `--provider` defaults to `$OSTIA_CAPTURE_PROVIDER`; `--instance-type` defaults to `$OSTIA_CAPTURE_INSTANCE_TYPE`, else the first line of the file `$OSTIA_CAPTURE_INSTANCE_TYPE_FILE` names. Other flags:

| Flag | Meaning |
| --- | --- |
| `--out <dir>` | An empty directory, or a previous capture's directory |
| `--print-id` | Run the whole capture in a private temporary directory and print only its `topo1` id; keeps nothing. Exclusive with `--out` |
| `--links <links.json>` | Measured links to include ([below](#measured-links)) |
| `--node-index <n>` | The node's index in a pair, recorded in `diagnostics.txt` only |
| `--extra-identifiers <file>` | More identifiers for the leak check, one per line |
| `--build-dir <dir>` | A build tree that holds the tool |

Put every site-specific name in `--extra-identifiers`: the data-centre or rack name, an asset tag, an account or project name, a cluster name. The leak check finds identifiers it reads from the machine; it cannot know a name only you know.

### On a Kubernetes GPU node

```bash
pixi run ostia-dev remote k8s --context <ctx> --profile l4 --suite topo-capture
```

The `topo-capture` suite runs `topo capture` in the pod. The pod's node name (one whole value), the kube context and the kubeconfig's cluster name (each as a whole value and as long segments) go into the leak check automatically, so none of them can appear in the capture. The runner fetches the capture before it removes the pod and leaves it in `build/remote/<run-id>/capture/` ([remote-runs.md](remote-runs.md#topology-captures)). The runner reads the instance type from the label of the node the pod landed on, so a node group that scales from zero is detected too; add `--env-var OSTIA_CAPTURE_INSTANCE_TYPE=<type>` if the node has no `node.kubernetes.io/instance-type` label or you can't read nodes. A failed capture fails this suite; a capture step in a gate never fails the run.

## What a fixture publishes

Only fields on the RFC-0003 §2 allowlist are written, and the tool validates its own output against closed schemas before the leak check. A fixture holds:

| File | Content |
| --- | --- |
| `hwloc.xml` | The topology tree: types, sizes, PCI bus IDs, vendor and device IDs, link speed and width, `cpuset`s, NUMA nodes. OS devices (network interface names, which can be derived from a MAC) are dropped |
| `nvml.json` | Per GPU: PCI bus ID, model string, compute capability, memory size, CUDA ordinal (the GPU's rank by PCI bus ID); NVLinks; the P2P matrix. Required when hwloc lists an NVIDIA GPU |
| `nics.json` | Per NIC: PCI bus ID, driver, link layer, maximum PCIe speed, port speed, NUMA node, and ibverbs facts. `rdma_probe` says whether the probe could run |
| `meta.json` | Provider, instance type, driver and CUDA versions, tool version, capture date |
| `links.json` | Measured bandwidth, when you passed `--links` |
| `manifest.json` | The artifact manifest, written last |
| `expected.json` | The golden model, added by `topo import` |

It never holds MACs, IPs, GUIDs, GIDs, UUIDs, serial numbers, hostnames, firmware versions or VPD. `diagnostics.txt` says what was read and skipped, without values; it helps debugging and is never committed.

### Provider names

Use `gcp`, `aws`, `azure` or `onprem` as the provider, and the cloud's own instance type (`g2-standard-16`, `g6.4xlarge`). The import slugs them: `gcp` and `g2-standard-16` become `captured/gcp-g2-standard-16`, and `g6.4xlarge` becomes `g6-4xlarge`. An on-prem machine is `captured/onprem-<system>`, such as `captured/onprem-dgx-h100`. A rented machine from another provider uses that provider's name.

## The manifest and exit codes

`manifest.json` says whether the capture is complete and safe. Consumers publish a capture only when `sanitized` is `true` and `leak_check` is `passed`.

| Field | Values |
| --- | --- |
| `schema` | `1` |
| `tool_version` | The tool's version |
| `status` | `complete`, `partial` or `failed` |
| `sanitized` | `true` only after the output passed its schemas |
| `leak_check` | `not_run`, `passed` or `failed` |
| `topology_id` | The `topo1` id of replaying the written files; `null` unless the exit code is 0 or 2 |
| `files` | The SHA-256 of each written file's bytes |
| `missing` | `{file, reason}` for each required file that was not written |
| `errors` | `{code, message}`, never a value from the machine |

A failed capture still writes a manifest, with `topology_id` `null` and the reason in `missing` or `errors`.

| Exit code | Meaning | Capture usable |
| --- | --- | --- |
| 0 | Complete | Yes |
| 1 | Failed, or a usage error | No |
| 2 | Partial: a required source failed (for example NVML on a GPU machine) | For debugging; not importable |
| 3 | A leak or schema violation; the data files are removed | No |

The same codes apply to `ostia-dev topo capture`, with one addition: its own failures before the tool runs (the tool is missing or not executable, or the machine is not Linux) also exit 2, with a contract error and no capture line. Otherwise 2 means a partial capture. The codes also apply to `remote` runs ([remote-runs.md](remote-runs.md#topology-captures)).

## Add a fixture

Review the capture, then import it:

```bash
pixi run ostia-dev topo import capture/                 # fixture name from meta.json
pixi run ostia-dev topo import capture/ --name onprem-dgx-h100
pixi run ostia-dev topo show fabric/tests/fixtures/topology/captured/onprem-dgx-h100
```

`topo import` accepts only a complete capture that passed the manifest checks, scans it again for leaks, copies the files the manifest lists plus `manifest.json`, and writes `expected.json`. It never overwrites an existing fixture, and it prints a checklist for your review: the provider and instance type, the driver and CUDA versions, that `topo show` matches the machine, that `expected.json` is reviewed like code, and that no site name or asset tag appears in the files. Then run `pixi run ostia-dev lint` (it runs the fixture-leaks check) and open a pull request.

### Measured links

After a benchmark run on the captured machine, `topo links` converts its unidirectional `p2p_copy` records into `links.json`:

```bash
pixi run ostia-dev topo links bench/results/<run-id>/results.jsonl --capture capture/ --out links.json
```

Records carry the bus IDs they measured in a `devices` field, so CUDA ordinals are never mapped. Records whose source and destination bus IDs are equal (a same-device copy) are skipped. The link kind comes from the capture's NVLinks, and there is one entry per source, destination and transfer size. Pass the file to `topo capture --links` to include it.

## Compare and find topologies

```bash
pixi run ostia-dev topo diff <a> <b>        # exit 0 when the two topo1 ids are equal, 1 when they differ
pixi run ostia-dev topo which <id-or-prefix>   # the fixtures whose id is, or starts with, this
```

`diff` takes a fixture or a capture directory on each side. `which` takes a `topo1:sha256:` id or a prefix of its hex digest; `bench compare` prints the 12-character form in its `skipped` message when two results were measured on different topologies, and points at this command.

## Leak check

The tool collects identifiers from sources independent of what it wrote to the output, then searches every output file, `diagnostics.txt` included, for each of them in the forms they take (separators and case normalised, EUI-64 and IPv4-mapped GIDs, and so on). The raw set holds:

- sysfs network addresses, InfiniBand GUIDs and GIDs, and NIC VPD serial keywords;
- the identifying DMI fields only: serial numbers, UUIDs, asset tags and instance-ID-shaped values;
- live interface addresses, the hostname and FQDN, and `/etc/machine-id`;
- NVML UUIDs, serial numbers and board IDs;
- every line of `--extra-identifiers`.

Hex identifiers match case- and separator-insensitively. Names, IPs and `--extra-identifiers` lines match as whole tokens (a multi-word line as an anchored substring). A value shorter than six characters after normalising is skipped and counted in `diagnostics.txt`. Loopback and unspecified addresses, and all-zero or all-`f` values, are skipped.

### Triage a finding

A finding is `file:line locator: kind`, never the value. The locator is a JSON pointer for a `.json` file and an element path for `hwloc.xml` (attribute names follow `/@`). The kind is `mac`, `guid`, `gid`, `ipv4`, `ipv6`, `ipoib`, `uuid`, `serial`, `hostname`, `machine-id`, `dmi`, `instance-id` or similar.

1. On exit 3 no data file reaches `--out`; the files are staged and removed. Read the `leak:` lines in the capture's `diagnostics.txt`, each `<file>:<line> <locator>: <kind>`, and judge from the machine's side whether that field should hold a value of that kind.
2. **A real leak** (an identifier passed the schemas): do not publish. Report it privately as `SECURITY.md` describes, GitHub private vulnerability reporting first, with file, locator and kind only.
3. **A false positive** (the field is structural and only looks like an identifier, for example a short hostname that equals a token of a GPU model string): open an ordinary public issue with file, locator and kind only. The check has no flag to loosen it.

Never include the value itself in a report.

A `--require-topology` run that fails with capture exit 3 is a finding of this check.

Known limit: the node name is searched as one whole value, and the kube context and the kubeconfig's cluster name are searched whole and as long segments; segments shaped like cloud regions or zones (a word pair and at most two digits, such as `us-central1-a`, `ap-southeast-1a` or `europe-west4`) and short or provider-word segments are skipped, because every account shares them; a numbered project id such as `my-project-123456` is kept. Whole values are still searched. Put a distinctive site name in `--extra-identifiers` if it would otherwise survive.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `ostia-topo-capture was not found` | Build it (`pixi run ostia-dev build` on Linux), or set `$OSTIA_TOPO_CAPTURE` or `--build-dir` |
| `topo capture` says it needs Linux | The tool reads sysfs and loads NVML; run it on the machine, or `remote k8s --suite topo-capture` |
| Exit 2, `nvml.json` missing | NVML failed or `libnvidia-ml.so.1` is not visible to the tool; check `nvidia-smi` runs as the same user |
| Exit 2 on a node with a GPU you did not expect | hwloc lists an NVIDIA GPU, so `nvml.json` is required |
| `rdma_probe` is `unavailable` | `libibverbs.so.1` did not load or `ibv_get_device_list` failed; the capture is still complete |
| Exit 3 | The leak check or a schema failed; see [Leak check](#leak-check) |
| Exit 1 with `--provider` or `--instance-type` | `--out` needs both; set the flags or the two `OSTIA_CAPTURE_*` variables |
| `bench run` records have `topology: null` | The driver gives `--print-id` 120 seconds; on a timeout or failure it warns and writes `null`, or fails under `--require-topology`. Run `topo capture --print-id` by hand to see why |
| `topo import` refuses | The capture is not complete, was not accepted, or the target exists; the error names the rule |
| `topo import` asks for `--name` | `meta.json` records the provider or instance type as `unknown` (a `--print-id` default); pass `--name <provider>-<instance-type>` |
