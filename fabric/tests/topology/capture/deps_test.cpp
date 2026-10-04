#include <cstring>
#include <gtest/gtest.h>
#include <string>

#include "core/diagnostics.hpp"
#include "core/result.hpp"

#ifdef __linux__
#include <infiniband/verbs.h>
#endif

using ostia::topo_capture::Diagnostics;
using ostia::topo_capture::Result;

TEST(CaptureDeps, DiagnosticsRecordsAndRenders) {
    Diagnostics d;
    d.add("skipped 2 short values");
    d.add("nvml: absent");
    ASSERT_EQ(d.lines().size(), 2U);
    EXPECT_EQ(d.render(), "skipped 2 short values\nnvml: absent");
}

TEST(CaptureDeps, ResultHoldsAValueOrAnError) {
    Result<int> good(7);
    ASSERT_TRUE(good.ok());
    EXPECT_EQ(good.value(), 7);
    Result<int> bad = Result<int>::failure("nvml.load");
    ASSERT_FALSE(bad.ok());
    EXPECT_EQ(bad.error(), "nvml.load");
}

TEST(CaptureDeps, ToolVersionIsDefined) { EXPECT_GT(std::strlen(OSTIA_TOOL_VERSION), 0U); }

#ifdef __linux__
// RFC-0003 §1: the probe compiles against rdma-core headers and dlopens the library.
static_assert(sizeof(ibv_port_attr) > 0);
#endif
