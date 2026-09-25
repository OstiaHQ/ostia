# Ostia PRD: GPU-Centric Data Infrastructure

Last updated 2026-09-25 · Owner: @ShAlireza

> This file is the source of truth for the PRD. Change it through a pull request (see [docs/README.md](../README.md)).

## Summary

Ostia is a GPU-centric data infrastructure stack, built in three layers, that lets engines move and process large relational datasets across GPUs at close to hardware speed. Layer 1 is an embeddable data-movement library. Layer 2 is a Spark-like distributed runtime built on Layer 1. Layer 3 is a distributed GPU query engine built on Layer 2.

Ostia's edge is that it **plans around the hardware it finds**. It discovers the topology (NVLink, NVSwitch, PCIe, NUMA, NICs), measures links, picks transports, routes over several paths at once and decides where to run operators while data is in flight. Existing GPU communication stacks (NCCL, UCX, NIXL, rapidsmpf) treat the fabric as a dumb pipe, handle only bytes or collectives, or ignore topology.

Ostia is a product rewrite of Magi, a Master's research prototype that also appeared in a paper; the new name keeps the two apart. The name comes from Ostia, the harbour that every shipment into ancient Rome passed through. It keeps the prototype's ideas and none of its code structure. Layer 1 is built first; Layers 2 and 3 are sketched here so Layer 1's contracts serve them.

## Background: lessons from the prototype

The prototype (repo `fardatalab/Magi`; work stopped around 2025-09-19) showed the core ideas pay off, but its code can't be turned into a product. Its control plane was written for many machines, while its data path only worked inside one machine.

| Area | What the prototype did | Verdict for the new project |
| --- | --- | --- |
| Topology discovery | hwloc + NVML + sysfs graph of NVLink/NVSwitch, PCIe gen, NUMA/UPI, NICs (`src/agent/local_topo.cu`) | **Keep the idea.** Add measured bandwidth; drop hardcoded bandwidth tables |
| Multipath routing | Max-flow (Edmonds–Karp) + flow decomposition, run once per channel (`src/coordinator/max_flow.cu`) | **Keep the idea.** Must account for other traffic, adapt at runtime, and work across nodes |
| Device-side send/receive | Warp-cooperative slot reservation, 16-byte vectorized copies, zero-copy receive (`include/worker/interface.cuh`) | **Keep the technique** for pack kernels and zero-copy receive |
| On-path processing | Aggregation at a middle hop took 1.28 s vs 1.77 s at the destination (16 GiB, 4 streams). Filter wins only at 8 or more streams | **Keep as an optimizer choice** (where an operator runs), not a core primitive |
| Throughput tuning | Pipelining roughly doubled throughput (22 → 44 GB/s); batching/packet size to 44 GB/s; dual-link aggregation to 90 GB/s; GPUDirect RDMA near 100 Gbps line rate | **Keep as design rules:** pipelined chunks, large packed messages, multi-rail |
| Data plane | CUDA IPC + P2P only. UCX registered buffers but never exchanged rkeys; `ucxmagi/` RDMA library never wired in | **Rewrite** as Layer 1a with pluggable transports |
| User API | `interface.h` (`channel_create`, device `put`/`get`, UDF chains) designed but never implemented; on-path UDFs passed to the GPU but never executed | **Redesign** as Layer 1b |
| Robustness | No backpressure (senders never check for space), untyped raw-struct TCP protocol, bare `throw "string"`, fixed 1024-entry arrays with no bounds checks, hardcoded ports, row type and NIC count | **Rewrite** with credits, a versioned protocol and typed errors |
| Real workload | MGjoin (2-GPU distributed hash join) used its own UCX wrapper, not the prototype's | **First validation workload** for Layer 1b |

One thing the prototype never tested: whether running an operator at a middle hop beats running it at the **source**. Pushing a filter or aggregate down to the source usually wins, because fewer bytes cross every hop. On-path mainly wins at convergence points (tree aggregation, merging skewed keys) or when the source GPU is busy with compute.

## Goals, non-goals, users and hardware

Ostia is product-grade infrastructure that others depend on. API stability, failure semantics, ease of deployment and ease of debugging come before research novelty.

**Goals**

- Move columnar data between GPUs at close to link speed on whatever hardware is present, with no data passing through host memory unless the planner chooses to (e.g. spilling).
- Plan transfers from the discovered and measured topology: transport choice, several paths at once, operator placement.
- Define clear failure semantics that a runtime above can build fault tolerance on.
- Give each layer a stable, versioned contract so the layers above can evolve independently.

**Non-goals for Layer 1**

- Owning processes, GPUs or job scheduling (that's Layer 2).
- Query planning or SQL (that's Layer 3).
- Recovering from failures itself. Layer 1 detects and reports failures; Layer 2 recovers.
- Collectives for ML training (NCCL already covers them).

**Target users**

- GPU database and data-engine builders who embed Layer 1, starting with our own Layers 2 and 3.
- Later: data teams submitting jobs to Layer 2, and analysts running queries on Layer 3.

**Target hardware: all of it, detected at runtime**

| Environment | Example | Expected transports |
| --- | --- | --- |
| One node, many GPUs | 8×H100/B200 on NVSwitch | NVLink peer-to-peer copies, CUDA IPC |
| Clusters with an RDMA fabric | Multi-GPU nodes on InfiniBand or RoCE | UCX with GPUDirect RDMA, several NICs at once |
| Commodity cloud | Mixed GPUs, TCP or AWS EFA | UCX over TCP, libfabric for EFA, staged through host memory |

## Layered architecture

Ostia is four layers with one-way dependencies. Each layer uses only the public, versioned contract of the layer directly below it. Layer 1 splits into Fabric (1a: bytes and hardware) and Exchange (1b: typed columnar data), so transport code never knows about schemas and exchange code never knows about NIC queues.

![Ostia layer stack · 4 layers on the hardware](figures/01-layer-stack.svg)

Layers 1a and 1b are built first. Layers 2 and 3 are sketched now so Layer 1's contracts don't paint them into a corner. Each layer keeps its colour from this figure in every diagram below.

| Contract | Provider → consumer | What crosses it | What does not |
| --- | --- | --- | --- |
| Fabric API | 1a → 1b | Topology graph with measured links; registered memory; point-to-point send, receive and put of bytes; completion and error events | Schemas, partitioning, retries |
| Exchange API | 1b → Layer 2 | Exchange handles (shuffle, broadcast, gather) over Arrow device batches; pushdown operator hooks; statistics; control over how long shuffle output lives; typed failure events | Process lifecycle, task scheduling, recovery |
| Runtime API | Layer 2 → Layer 3 | Stage and task graphs, worker pools, job submission, fault-tolerant execution, resource placement | SQL, cost-based query planning |
| User API | Layer 3 → users | SQL and dataframe queries, catalogs, results | — |

### Components and repositories

Each layer ships as a plainly named component, `ostia-<role>`. Repositories are split by license, not by layer, and layering is enforced by the build.

| Component | Layer | C++ namespace / CMake target | C ABI prefix | Python package |
| --- | --- | --- | --- | --- |
| ostia-fabric | 1a | `ostia::fabric` | `ostia_fabric_*` | `ostia-fabric` (`import ostia.fabric`) |
| ostia-exchange | 1b | `ostia::exchange` | `ostia_exchange_*` | `ostia-exchange` |
| ostia-runtime | 2 | `ostia::runtime` | `ostia_runtime_*` | `ostia-runtime` |
| ostia-query | 3 | `ostia::query` | `ostia_query_*` | `ostia-query` |
| ostia-telemetry | all | `ostia::telemetry` | `ostia_telemetry_*` | `ostia-telemetry` |

`pip install ostia` installs the full open stack. Shared libraries follow the component names, e.g. `libostia-fabric.so`.

- **`ostiahq/ostia`**, public, Apache-2.0: one monorepo with `fabric/`, `exchange/`, `telemetry/`, and the community editions of `runtime/` and `query/`. It's released together at first, with per-component versions later if release cadences drift.
- **`ostiahq/ostia-enterprise`**, private, commercial license: enterprise features for Layers 2 and 3. It depends only on *released* versions of the public repo, never on Layer 1 internals.
- **How layering is enforced:** CMake targets with private include folders, so `fabric` can't include `exchange` headers; a CI check that fails on any upward dependency; each layer's public headers as its only contract, with ABI stability checks.
- **When to split further:** ostia-fabric gains outside users who want it alone, a layer needs its own release cadence, or Layers 2 and 3 move to Rust or Go.

This follows common practice. Layered stacks such as UCX, Ray, Spark and LLVM live in one repo while their contracts evolve together. Projects like Arrow, DataFusion and DuckDB split parts out once those parts had their own cadence, maintainers or license.

**Third-party code.** Ostia builds on open-source libraries such as UCX, hwloc, Apache Arrow, NVIDIA CCCL and OpenTelemetry C++. The open-source core only takes dependencies under permissive licenses (BSD, MIT, Apache-2.0), and they're listed in a `THIRD_PARTY_NOTICES` file. NVIDIA's proprietary components (the CUDA runtime, NVML and nvCOMP) are linked at runtime, not bundled; their redistribution terms are checked before any binary release.

## Layer 1a: ostia-fabric

Fabric moves **bytes** between any two memory locations in the cluster over the best available transport, and describes the hardware to the layers above as a measured graph. It has no coordinator: Layer 2 (or a simple standalone bootstrap) hands it the peer list, and topology is exchanged among all peers at startup.

![Layer 1a Fabric · components, backends, and what 1b calls](figures/02-fabric.svg)

Layer 1b only ever sees the top edge of this picture: a measured graph and a byte API. A put goes into the command queue, the progress engine sends it on the backend the planner chose, and any failure comes back as a typed event from the health monitor. Dashed backends come after v1.

**Decided**

- Bytes only: no schemas or partitioning in 1a.
- Transports are pluggable backends picked at runtime from what's detected.
- Stable identities: host ID plus NVML device UUID. Never IP-derived, unlike the prototype's `(ip << 32) | pcie_id` IDs.

**Decided in D5: who starts transfers**

- Transfers are started from the host and ordered on CUDA streams, like NCCL. A CPU progress thread drives the transports; GPUs do all data work. Every backend takes work from a command queue. In v1 only the host fills it. Later, device code can fill the same queue: a CPU proxy serves it over TCP and EFA, and it's served directly, with no CPU involved, on NVLink peer memory and IBGDA. Device-initiated transfers then become an addition, not a redesign.

| Component | Responsibility | v1 scope |
| --- | --- | --- |
| Topology discovery | Graph of GPUs, NUMA nodes, PCIe switches, NVSwitch, NICs, NVMe, from hwloc + NVML + sysfs + ibverbs | New implementation of GPU, PCIe, NUMA and NIC discovery |
| Link probing | Short microbenchmarks per link class at startup, cached per host fingerprint; replaces the hardcoded bandwidth tables | Bandwidth and latency per direction |
| Transport backends | One interface: capabilities, memory registration, connect, send/recv/put, progress | CUDA P2P/IPC in a node; UCX (GPUDirect RDMA, RoCE, TCP) across nodes. Later: libfabric for EFA, NVSHMEM/IBGDA, GDS |
| Memory manager | Registration cache, pools of pre-registered GPU and pinned host buffers, pluggable allocator (RMM-compatible) | GPU pool + host staging pool |
| Multi-rail | Stripe one large transfer across several NICs or NVLink paths | Static striping by measured bandwidth |
| Health | Peer liveness, timeouts, typed error events | Detect and report only; no recovery |

API sketch (illustrative, C++):

```cpp
auto fab = ostia::fabric::init({.peers = peer_list, .self = my_rank});
const ostia::Topology& topo = fab.topology();          // measured cluster graph
auto mr  = fab.register_memory(dev_ptr, bytes);         // or fab.pool().get(bytes)
auto ep  = fab.endpoint(peer_rank);
auto op  = ep.put(mr, offset, remote_key, bytes, stream); // stream-ordered
op.on_complete([](ostia::Status s) { /* typed error on failure */ });
```

## Layer 1b: ostia-exchange

Exchange moves **typed columnar data** between groups of GPUs. It supports shuffle (all-to-all by key), broadcast and gather. A planner decides how bytes move; flow control guarantees data is never overwritten. It's built only on the Fabric API.

![Layer 1b Exchange · one shuffle, sender to receiver](figures/03-exchange.svg)

Layer 2 only hands in Arrow batches and takes out zero-copy views. In between, 1b uses Fabric's topology to plan, its registered memory and puts to move packed partitions, and its completions to return credits. A sender never writes to the receiver unless it holds credit for that space.

**Decided**

- The data model is Arrow-compatible columnar batches on the device. Raw bytes exist only in 1a.
- Interop goes through the Arrow C Device Data Interface (`ArrowDeviceArray`). libcudf is an optional adapter, not a core dependency.
- Types come in phases. v1: fixed-width types, validity bitmaps, strings. Later: decimals, dictionaries, nested lists and structs.
- Design rules:
    1. A fused partition-and-pack kernel writes each partition into one contiguous, 64-byte-aligned buffer in pre-registered send memory. One message per partition, no extra copy.
    2. The receiver gets zero-copy column views into the received buffer. Combining batches is optional.
    3. Partitioning and sending are pipelined by chunk, so partition work hides behind the transfer.
    4. Estimated partitioning overhead is under 5% of transfer time across nodes and about 30–40% within a node over NVLink, hidden by pipelining. Both must be measured in M1.

| Component | Responsibility | v1 scope |
| --- | --- | --- |
| Planner | For each exchange: transport per peer pair, multipath split (max-flow on *remaining* link capacity), chunk size, compression, where to run operators | Plan fixed for the whole exchange; a ledger of link reservations shared by concurrent exchanges |
| Partitioner | Hash and range partitioning on key columns; histograms for skew | Hash on fixed-width and string keys |
| Flow control | Credit-based: receivers advertise buffer credits, senders wait at chunk boundaries | Per-peer credits; no unbounded writes |
| Spill and lifetime | Tiered GPU → pinned host → NVMe under memory pressure; output kept until the caller releases it | GPU → host spill; explicit release |
| Pushdown operators | Projection, filter, compression (nvCOMP); then partial aggregation; placement at the source, a middle hop or the destination | Projection, filter, compression at the source |
| Statistics | Bytes per partition, skew, achieved bandwidth per path | Exposed to Layer 2 and the planner |
| Failure events | Exchange-level typed errors naming the affected peers and partitions | Fail fast; Layer 2 decides whether to retry |

API sketch (illustrative):

```cpp
auto ex = ostia::exchange::shuffle(fab, {
    .schema = schema, .keys = {"l_orderkey"}, .partitions = 256,
    .senders = group, .receivers = group,
    .pushdown = {ostia::project({"l_orderkey", "l_extendedprice"}),
                 ostia::compress::automatic()}});
ex.send(arrow_device_batch, stream);   // repeat per batch
ex.finish_sends();
for (auto& batch : ex.receive(my_partition)) { /* zero-copy views */ }
ex.release();                           // Layer 2 controls lifetime
```

## Layer 2: ostia-runtime (initial sketch)

The Runtime is a Spark-like distributed execution engine for GPU clusters. It owns worker processes and GPUs, schedules stage graphs, recovers from failures and accepts user-submitted jobs. It uses Layer 1b for every data exchange and Layer 1a's topology for placement. Nothing here is decided yet; it will get its own spec after Layer 1 reaches M2.

![Layer 2 Runtime · control plane over per-GPU workers](figures/04-runtime.svg)

Each worker embeds Layer 1 as a library: stage tasks run GPU kernels, and data crosses stage boundaries only through 1b exchanges between workers. The control plane never touches data. When a worker fails, recovery re-runs its lost tasks from shuffle output that 1b kept for exactly this case.

| Component | Initial direction |
| --- | --- |
| Control plane | A driver per job plus a cluster manager. The cluster manager is the only place the prototype's coordinator role survives, and it holds no data-path state |
| Workers | One process per GPU; holds a Fabric instance and GPU memory budgets |
| Job model | Directed acyclic graph of stages; a stage is a GPU task set, and stage boundaries are Layer 1b exchanges |
| Scheduler | Places tasks by topology and data locality; asks the 1b planner for exchange cost estimates |
| Fault tolerance | Lineage-based re-execution, as in Spark. Shuffle output is kept (or spilled) through 1b's lifetime control until downstream stages commit |
| Membership | Heartbeats; workers can join or leave between stages |
| Submission | C++ and Python client APIs; integrations with Kubernetes and Slurm later |

**Open questions:** lineage vs checkpointing for long pipelines; making the cluster manager highly available (for example with Raft) vs restarting it; supporting several jobs sharing one cluster in v1 or not.

## Layer 3: ostia-query (initial sketch)

The Query engine turns SQL and dataframe queries into Layer 2 stage graphs of GPU operators, with Layer 1b exchanges at stage boundaries. Its advantage is a cost model that knows the real fabric: it can choose broadcast vs shuffle join, where to push operators into an exchange, and whether to compress, using the 1b planner's estimates. Nothing here is decided yet.

![Layer 3 Query engine · SQL to stage graph, with an example plan](figures/05-query.svg)

The optimizer's cost model gets exchange costs from the 1b planner, which works them out from 1a's measured topology. That lets it choose, for example, to push a filter and compression into the shuffle instead of running them as separate steps. The finished plan goes to Layer 2 as a stage graph.

| Component | Initial direction |
| --- | --- |
| Front end | Reuse an existing SQL parser and logical plan format (for example Substrait, Apache DataFusion or DuckDB) instead of writing our own |
| Optimizer | Rule-based first, then cost-based with fabric-aware exchange costs from Layer 1b |
| Operators | GPU scan, filter, project, hash join, hash aggregate, sort; kernels from libcudf or our own, behind an operator interface |
| Exchange pushdown | Filters, projections, partial aggregates and compression pushed into 1b exchanges; the planner picks source, middle hop or destination |
| Storage | Parquet on local NVMe or object storage; GPUDirect Storage later |
| First benchmark | TPC-H at scale factors 100 to 1000 against Spark RAPIDS and a single-node GPU engine |

**Open questions:** build on an existing engine (DataFusion, DuckDB/Sirius) as the front end vs our own; which data formats and catalogs v1 supports.

## Observability: ostia-telemetry (all layers)

Every layer is instrumented from M0. The developer's build choice sets how much is compiled in, and inside a build, tracing can be switched on at runtime and sampled. An `off` build carries no telemetry code and is the performance baseline.

**Two kinds of internal data.** Functional statistics are always on because the system needs them: partition sizes and skew for the planner, achieved link bandwidth for re-planning, credit state, and typed errors with context (peer, exchange, partition, path). Telemetry (metrics, traces, debug checks) serves only people observing the system, and it's optional.

| Build level | What's compiled in | Target overhead vs `off` |
| --- | --- | --- |
| `off` | No telemetry: instrumentation macros compile to no code | 0% (baseline) |
| `metrics` | Counters and histograms in every layer, exported through OpenTelemetry | ≤ 2% throughput |
| `trace` | Plus coarse spans, fine-grained event rings, NVTX ranges; tracing switched on at runtime (environment variable or API) with a sampling rate, e.g. 1% of exchanges | ≤ 2% when switched off, ≤ 10% when on |
| `debug` | Plus bounds checks, credit invariant checks, canary bytes on buffers, verbose logs | No limit |

![Observability · where signals are recorded and where they go](figures/06-observability.svg)

**Rules**

- **Metrics:** hot paths increment local counters only: per-thread on the CPU, per-warp in shared memory on the GPU (flushed when the kernel ends). OpenTelemetry *asynchronous (observable) instruments* read them on each export interval and send them over OTLP. The OpenTelemetry SDK is never called on a hot path.
- **Traces in two tiers:** coarse spans (query, stage, task, exchange: thousands per job) go to OpenTelemetry traces. Fine-grained events (chunk, transfer, credit wait, pack kernel, spill: up to millions per second) go as fixed-size binary records into per-thread and per-GPU ring buffers, exported to Perfetto and NVTX for Nsight Systems. Every fine event carries its exchange's span ID, so a slow exchange in the OpenTelemetry view opens into its detailed timeline.
- **One chain of trace IDs** runs through all layers: query → stage → task → exchange → chunk → transfer. In `trace` builds the IDs travel in 1a message headers; in other builds those header bytes don't exist.
- **Swappable flavours:** instrumentation is internal to Ostia, and every flavour exposes the same C ABI. A developer changes flavour by swapping the library, not recompiling their app.
- **Enforced in CI:** benchmarks run on `off`, `metrics`, and `trace` switched off, and fail if the overhead limits are exceeded.

| Layer | Key signals |
| --- | --- |
| 1a Fabric | Achieved bandwidth per path, queue depth, progress-loop latency, registration-cache hit rate, transport errors |
| 1b Exchange | Stall time waiting for credit, bytes per partition and skew, spilled bytes, compression ratio, pack/transfer overlap, planned vs actual path split |
| 2 Runtime | Task timeline, scheduling delay, retries, lineage recomputation |
| 3 Query engine | `EXPLAIN ANALYZE` with actual time and rows per operator; estimated vs actual exchange cost |

## Competitive landscape

No existing stack combines planning from measured topology, multipath routing and operator placement for relational data; that combination is Ostia's space. The rows below are from memory and not yet checked against current project pages. Verifying them is an M0 task.

| System | What it is | Overlap with Ostia | Gap Ostia fills |
| --- | --- | --- | --- |
| NCCL | NVIDIA collectives for ML (all-reduce, all-to-all), topology-aware rings and trees | Topology detection, GPUDirect transports | Bytes and collectives only; no columnar data, spilling, flexible membership or operator pushdown |
| UCX / UCXX | General transport framework (RDMA, TCP, shared memory, CUDA) | Ostia's main 1a backend | Point-to-point only; no planner, no multipath planning across the topology |
| NVIDIA NIXL | Transfer library for moving inference data (KV cache) with UCX and GDS backends | Pluggable transports, memory registration | Aimed at inference; bytes only; no relational exchange |
| RAPIDS rapidsmpf | Multi-GPU shuffle with UCXX and spilling, used by cuDF and Dask | Closest to 1b | No topology-driven multipath planning or on-path placement (to verify) |
| Spark RAPIDS + UCX shuffle | GPU plugin for Spark, with a UCX shuffle manager | Layers 2 and 3 | Built on the JVM and Spark's CPU-first runtime; the GPU is an accelerator, not the centre |
| NVSHMEM / IBGDA | GPU-initiated one-sided communication | Possible later device-initiated backend | Low-level; no data model |
| Theseus, Sirius (GPU DuckDB) | GPU SQL engines | Layer 3 | Closed source (Theseus) or single-node focus (Sirius, to verify) |

The strategy is to **interoperate, not compete**. UCX, and later NIXL or NVSHMEM, are backends under 1a, and Arrow and libcudf are interop targets for 1b. The value is in the planner and the exchange semantics.

## Roadmap and milestones

The roadmap runs in six phases, each ending in a measurable gate, and Layer 1 (M1–M3) comes first. Durations aren't set yet; each layer gets its own spec and plan before its phase starts.

![Roadmap · 6 phases, a gate after each](figures/07-roadmap.svg)

| Phase | Scope | Gate (exit criteria) |
| --- | --- | --- |
| M0 Foundations | New repo, CMake + CI, benchmark harness, telemetry framework (build levels, counters, trace rings, OpenTelemetry export), decision log, verified competitive landscape. Cheap testing without clusters: topology fixtures captured from real and rented machines so the planner can be tested offline; multi-process tests on one GPU over UCX loopback and TCP; scripted rent-run-teardown for gate benchmarks | Harness reproduces the prototype's baselines: P2P NVLink bandwidth, GPUDirect RDMA near line rate |
| M1 Fabric v1 | Topology discovery + probing, CUDA P2P/IPC and UCX backends, memory pools, stream-ordered byte API, health events | ≥ 90% of measured link bandwidth on NVLink and GPUDirect RDMA; killing a peer produces a typed error, not a hang |
| M2 Exchange v1 | Columnar shuffle, broadcast, gather; fused partition+pack; credits; GPU → host spill; projection, filter and compression pushdown; fixed multipath plan per exchange | MGjoin ported to Ostia and faster than the hand-written version; no data loss in slow-consumer and skew tests; compared against rapidsmpf |
| M3 Planner v2 | Link-reservation ledger for concurrent exchanges, adaptive re-planning, multi-rail striping, on-path partial aggregation placement | Measured gain from multipath and placement on multi-NIC or NVSwitch nodes |
| M4 Runtime v1 | Layer 2: driver, workers, stage graphs, lineage recovery, job submission | A multi-stage job finishes correctly after a worker is killed mid-shuffle |
| M5 Query engine v1 | Layer 3: SQL front end, GPU operators, exchange-aware optimizer | TPC-H at scale factor 100 or more end to end, compared against Spark RAPIDS |

## Decision log and open questions

All twelve foundational decisions are made. The next step is a detailed Layer 1 spec, then an implementation plan for M0 and M1. New decisions are recorded as ADRs in [`docs/adr/`](../adr/), numbered from 0013.

| # | Decision | Choice | Status |
| --- | --- | --- | --- |
| D1 | What Ostia is for | Product-grade infrastructure others depend on; stability and failure semantics before novelty | Decided |
| D2 | Target hardware | All of it (one node, RDMA clusters, commodity cloud), detected and planned at runtime | Decided |
| D3 | Layering | 1a Fabric (bytes) → 1b Exchange (columnar) → Layer 2 Runtime → Layer 3 Query engine | Decided |
| D4 | 1b data model | Arrow-compatible columnar device batches with the fused partition+pack rules; bytes only in 1a; libcudf optional | Decided |
| D5 | Who starts transfers | Host-started and stream-ordered in v1; backends take work from a command queue, so a device-side API (proxied over TCP and EFA, direct on NVLink peer memory and IBGDA) can be added later | Decided |
| D6 | Relation to the prototype | A new project built from scratch in the author's own GitHub org, `ostiahq`. The prototype is a design reference only: the ideas behind its topology discovery, max-flow multipath routing and warp-cooperative packing are reimplemented against the new interfaces | Decided |
| D7 | Language and bindings | Layer 1: C++20 + CUDA core, a stable C ABI, Python bindings via nanobind; sanitizers in CI, RAII handles, no owning raw pointers. Layers 2 and 3: open, and may use Rust or Go for the control plane over Layer 1's C ABI | Decided |
| D8 | License and governance | Open core. Layer 1 (1a + 1b) is Apache-2.0. Layers 2 and 3 have an open community edition plus a commercial enterprise edition; the split is set in each layer's spec. Contributors sign a CLA (not only a DCO) so dual licensing stays possible. The author is sole maintainer early on | Decided |
| D9 | Reference hardware for CI and benchmarks | No owned cluster; rent only when needed. Everyday CI: CPU runners plus one rented single-GPU runner. Gate benchmarks on rented reference setups: (1) a node with 2+ NVLink GPUs, (2) two nodes with GPUDirect RDMA NICs, 2 per node for multi-rail, (3) a TCP-only or AWS EFA pair. Provisioning is scripted so a benchmark run brings machines up and tears them down in one command | Decided |
| D10 | Observability | Built in from M0 in all layers. Build levels off / metrics / trace / debug; trace builds switch tracing on at runtime with sampling. OpenTelemetry for metrics (asynchronous instruments over local counters) and coarse spans; own binary ring-buffer tracer exported to Perfetto and NVTX for fine events. Functional statistics and typed errors are always on | Decided |
| D11 | Product name | Ostia, after ancient Rome's harbour (Portus and Cursus rejected because of existing projects in the same space). GitHub org ostiahq. PyPI, conda-forge and crates.io name ostia were free on 2026-09-25; candidate domains ostiadb.com or ostiadb.dev. To do before going public: trademark search (USPTO and EUIPO, classes 9 and 42), then reserve the PyPI and conda-forge names | Decided |
| D12 | Component names and repository layout | Plain names: ostia-fabric (1a), ostia-exchange (1b), ostia-runtime (2), ostia-query (3), ostia-telemetry (all layers). Public monorepo ostiahq/ostia (Apache-2.0) for all open components; private ostiahq/ostia-enterprise for enterprise Layer 2/3 features, depending only on released public versions. Layering enforced by CMake target visibility and a CI upward-dependency check. Split further only for a separate cadence, maintainers or language | Decided |

**Open questions**

- Does on-path placement beat pushing operators to the source outside convergence cases? The prototype's four-GPU placement runs should answer this; their results need to be found or re-run in M0.
- Which planner decisions can wait until M3 (adaptive re-planning, the link-reservation ledger) without making M2's API unstable?
- Where does membership change live: allow peers to join during an exchange in 1b, or only between exchanges, managed by Layer 2?
