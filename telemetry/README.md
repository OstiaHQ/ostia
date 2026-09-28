# ostia-telemetry

Metrics, traces and debug checks that compile out when off. Rank 0: every other component may depend on it, and it depends on nothing.

- Build levels and the flavour check: [RFC-0001 §5](../docs/rfcs/0001-m0-foundations.md#5-telemetry-build-levels).
- Runtime (counters, trace rings, OpenTelemetry export, C ABI): RFC-0002 ([PR #10](https://github.com/OstiaHQ/ostia/pull/10)).

In M0 so far it provides `ostia::Result<T>` (`<ostia/telemetry/result.hpp>`) and `ostia_telemetry_build_level()` (`<ostia/telemetry/telemetry.h>`).
