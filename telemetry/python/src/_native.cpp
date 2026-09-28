#include <nanobind/nanobind.h>

#include <ostia/telemetry/level_check.hpp>
#include <ostia/telemetry/telemetry.h>

NB_MODULE(_native, m) {
    // A flavour mismatch is an ImportError, never the end of the process (RFC-0001 §5).
    if (auto r = ostia::telemetry::detail::check_build_level("ostia-telemetry",
                                                             OSTIA_COMPILED_TELEMETRY_LEVEL);
        !r) {
        throw nanobind::import_error(r.error().message.c_str());
    }
    m.def("build_level", &ostia_telemetry_build_level,
          "Telemetry level of the loaded libostia-telemetry (RFC-0001 §5).");
}
