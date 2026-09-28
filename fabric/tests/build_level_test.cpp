#include <gtest/gtest.h>
#include <ostia/fabric/fabric.h>
#include <ostia/telemetry/telemetry.h>

// Fabric reaches telemetry through its real link (RFC-0001 §3.2).
TEST(FabricTelemetry, SeesTheLinkedTelemetryLevel) {
    EXPECT_EQ(ostia_fabric_telemetry_build_level(), ostia_telemetry_build_level());
}
