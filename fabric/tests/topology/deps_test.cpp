#include <gtest/gtest.h>
#include <hwloc.h>
#include <nlohmann/json.hpp>

// RFC-0001 §3.4: the model target builds everywhere, so both dependencies must resolve
// on every CI platform, including the no-pixi containers.
TEST(TopologyDeps, HwlocMeetsTheFloor) {
  // API versions do not track minor releases one-to-one; the 2.4 floor (D9) is enforced at
  // configure time in cmake/Dependencies.cmake.
  EXPECT_GE(hwloc_get_api_version(), 0x00020000u);
}

TEST(TopologyDeps, JsonSortsKeys) {
  nlohmann::json j = {{"b", 1}, {"a", 2}};
  EXPECT_EQ(j.dump(), R"({"a":2,"b":1})");
}
