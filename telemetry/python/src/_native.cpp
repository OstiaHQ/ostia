#include <nanobind/nanobind.h>
#include <ostia/telemetry/telemetry.h>

NB_MODULE(_native, m) {
    m.def("build_level", &ostia_telemetry_build_level,
          "Telemetry level of the loaded libostia-telemetry (RFC-0001 §5).");
}
