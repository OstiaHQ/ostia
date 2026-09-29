#include <ostia/telemetry/config.h>
#include <ostia/telemetry/telemetry.h>

namespace {
constexpr int kBuildLevel = OSTIA_TELEMETRY_LEVEL; // RFC-0001 §5
} // namespace

extern "C" int ostia_telemetry_build_level(void) { return kBuildLevel; }
