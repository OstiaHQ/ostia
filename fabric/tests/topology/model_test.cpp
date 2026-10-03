#include <gtest/gtest.h>

#include "topology/model.hpp"

using namespace ostia::fabric::topology;

TEST(Model, CanonicalJsonSortsNodesEdgesAndKeys) {
  Model m;
  m.nodes.push_back({NodeKind::gpu, "0000:0a:00.0", {{"model", std::string("L4")}, {"cc_major", 8}}});
  m.nodes.push_back({NodeKind::numa, "numa-0", {}});
  m.edges.push_back({EdgeKind::numa_local, "0000:0a:00.0", "numa-0", {}});
  EXPECT_EQ(to_json(m).dump(),
            R"({"edges":[{"from":"0000:0a:00.0","kind":"numa_local","to":"numa-0"}],)"
            R"("nodes":[{"cc_major":8,"key":"0000:0a:00.0","kind":"gpu","model":"L4"},)"
            R"({"key":"numa-0","kind":"numa"}]})");
}

TEST(Model, DumpIsByteStableAcrossInsertionOrder) {
  Model a, b;
  Node g{NodeKind::gpu, "0000:07:00.0", {}}, n{NodeKind::nic, "0000:3b:00.0", {}};
  a.nodes = {g, n};
  b.nodes = {n, g};
  EXPECT_EQ(canonical_dump(a), canonical_dump(b));
}

TEST(Model, TopologyErrorFormatsWhat) {
  EXPECT_STREQ(TopologyError("schema", "a.xml", "bad").what(), "a.xml: bad (schema)");
  EXPECT_STREQ(TopologyError("xml", "", "bad").what(), "bad (xml)");
}
