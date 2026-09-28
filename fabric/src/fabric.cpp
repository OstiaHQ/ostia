#include <ostia/fabric/fabric.h>
#include <ostia/telemetry/telemetry.h>

extern "C" int ostia_fabric_telemetry_build_level(void) { return ostia_telemetry_build_level(); }
