#include <nanobind/nanobind.h>

#include <ostia/fabric/fabric.h>
#include <ostia/telemetry/level_check.hpp>

NB_MODULE(_native, m) {
    // A flavour mismatch is an ImportError, never the end of the process (RFC-0001 §5).
    if (auto r = ostia::telemetry::detail::check_build_level("ostia-fabric",
                                                             OSTIA_COMPILED_TELEMETRY_LEVEL);
        !r) {
        throw nanobind::import_error(r.error().message.c_str());
    }
    m.def("telemetry_build_level", &ostia_fabric_telemetry_build_level,
          "Telemetry level of the libostia-telemetry that libostia-fabric links (RFC-0001 §5).");
}
