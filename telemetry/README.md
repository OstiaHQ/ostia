# ostia-telemetry

Metrics, traces and debug checks that compile out when off. Rank 0: every other component may depend on it, and it depends on nothing.

- Build levels and the flavour check: [RFC-0001 §5](../docs/rfcs/0001-m0-foundations.md#5-telemetry-build-levels).
- Runtime (counters, trace rings, OpenTelemetry export, C ABI): [RFC-0002](../docs/rfcs/0002-telemetry.md).

In M0 so far it provides the following:
- `ostia::Result<T>` (`<ostia/telemetry/result.hpp>`).
- `ostia_telemetry_build_level()` (`<ostia/telemetry/telemetry.h>`), which reports the level the library was built at.
- The build levels: `OSTIA_TELEMETRY`, the generated `config.h`, the instrumentation macros and the flavour check. These are in `instrument/`, which is build-tree only and reached through `ostia::telemetry_config`.
- The catalog generator, `tools/gen_catalog.py`, which reads `telemetry.toml` in RFC-0002 §1's schema.
- The exported C ABI, listed in `abi/exports.txt`. It is identical in every flavour.
