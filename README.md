# Ostia

Topology-aware GPU data infrastructure: a fabric that moves bytes over the best path the hardware offers, a columnar exchange for shuffles, a distributed runtime and a query engine on top.

Ostia is at the design stage. Start with the [product requirements](docs/product/prd.md), and see [docs/README.md](docs/README.md) for how designs are proposed, reviewed and found.

| Component | Layer | Role |
| --- | --- | --- |
| ostia-fabric | 1a | Topology discovery, transports, memory registration, byte transfers |
| ostia-exchange | 1b | Columnar shuffle, broadcast and gather with flow control and a planner |
| ostia-runtime | 2 | Workers, scheduling, fault tolerance, job submission |
| ostia-query | 3 | SQL and dataframe queries on GPUs |
| ostia-telemetry | all | Metrics, traces and debug checks that compile out when off |

Licensed under the [Apache License 2.0](LICENSE).
