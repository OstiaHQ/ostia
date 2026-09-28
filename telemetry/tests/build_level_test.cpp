#include <dlfcn.h>
#include <filesystem>
#include <gtest/gtest.h>

#include <ostia/telemetry/telemetry.h>

// Fixed at metrics (1) until the build levels land (RFC-0001 §5, Rollout PR 3).
TEST(BuildLevel, IsMetricsUntilLevelsLand) { EXPECT_EQ(ostia_telemetry_build_level(), 1); }

// ctest must exercise the fresh build, never a copy installed into the pixi environment.
TEST(BuildLevel, LoadedFromBuildTree) {
    Dl_info info{};
    ASSERT_NE(dladdr(reinterpret_cast<void*>(&ostia_telemetry_build_level), &info), 0);
    auto loaded = std::filesystem::canonical(info.dli_fname).parent_path();
    EXPECT_EQ(loaded, std::filesystem::canonical(OSTIA_EXPECT_LIBDIR)) << info.dli_fname;
}
