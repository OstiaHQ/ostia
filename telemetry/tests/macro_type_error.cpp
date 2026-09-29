// Compiled with -fsyntax-only at level off by the telemetry.macro_type_error_at_off test:
// a type error inside a disabled macro must still fail to compile (RFC-0001 §5).
#include <ostia/telemetry/instrument.hpp>
#include <ostia/telemetry/telemetry_catalog.hpp>

static_assert(OSTIA_TELEMETRY_LEVEL == 0, "this probe checks the off level");

void probe() {
#ifdef OSTIA_PROBE_WELL_TYPED
    OSTIA_COUNT(ostia::telemetry::catalog::selftest_count, 1);
#else
    OSTIA_COUNT(ostia::telemetry::catalog::selftest, 1); // an event handle is not a metric
#endif
}
