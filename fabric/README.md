# ostia-fabric

Layer 1a: topology discovery, transports, memory registration and byte transfers. Rank 1; it may depend on telemetry.

- Repository layout and layering: [RFC-0001 §3](../docs/rfcs/0001-m0-foundations.md#3-repository-layout-and-targets).
- Topology model and fixture replay (M0): RFC-0003 ([PR #8](https://github.com/OstiaHQ/ostia/pull/8)).
- The fabric API itself comes with the ostia-fabric RFC.

In M0 so far it provides only `ostia_fabric_telemetry_build_level()`, which proves the link to telemetry.
