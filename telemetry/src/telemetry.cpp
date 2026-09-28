#include <ostia/telemetry/telemetry.h>

namespace {
// Fixed at metrics until the build levels land (RFC-0001 §5, Rollout PR 3).
constexpr int kBuildLevel = 1;
} // namespace

extern "C" int ostia_telemetry_build_level(void) { return kBuildLevel; }
