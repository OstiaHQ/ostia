#include <nanobind/nanobind.h>
#include <ostia/fabric/fabric.h>

NB_MODULE(_native, m) {
    m.def("telemetry_build_level", &ostia_fabric_telemetry_build_level,
          "Telemetry level of the libostia-telemetry that libostia-fabric links (RFC-0001 §5).");
}
