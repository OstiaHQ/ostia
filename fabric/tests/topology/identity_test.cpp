#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <ctime>
#include <gtest/gtest.h>
#include <map>
#include <string>
#include <utility>
#include <vector>

#include "topology/error.hpp"
#include "topology/identity.hpp"

using namespace ostia::fabric::topology;

static Model gpus_with_nvlinks(int n, const std::vector<std::pair<int, int>>& links) {
    Model m;
    auto key = [](int i) {
        char b[16];
        std::snprintf(b, sizeof b, "0000:%02x:00.0", 0x10 + i);
        return std::string(b);
    };
    for (int i = 0; i < n; ++i)
        m.nodes.push_back({NodeKind::gpu,
                           key(i),
                           {{"model", std::string("H100")},
                            {"cc_major", 9},
                            {"cc_minor", 0},
                            {"cuda_ordinal", i}}});
    for (auto [a, b] : links)
        m.edges.push_back({EdgeKind::nvlink, key(a), key(b), {{"links", 2}}});
    return m;
}

// Same shape, new bus IDs and ordinals.
static Model renumbered(const Model& m, unsigned seed) {
    std::vector<std::string> keys;
    for (auto& n : m.nodes)
        keys.push_back(n.key);
    auto shuffled = keys;
    // Fisher-Yates over a 32-bit LCG: libstdc++'s <random> pulls in SSE intrinsics that
    // `ostia-dev check macros` (libclang) cannot parse.
    std::uint32_t x = seed;
    for (std::size_t i = shuffled.size(); i > 1; --i) {
        x = x * 1664525u + 1013904223u;
        std::swap(shuffled[i - 1], shuffled[(x >> 16) % i]);
    }
    std::map<std::string, std::string> map;
    for (size_t i = 0; i < keys.size(); ++i)
        map[keys[i]] = shuffled[i];
    Model r = m;
    for (auto& n : r.nodes) {
        n.key = map[n.key];
        n.attrs["cuda_ordinal"] = std::int64_t(seed % 7);
    }
    for (auto& e : r.edges) {
        e.from = map[e.from];
        e.to = map[e.to];
    }
    return r;
}

TEST(Identity, HasPrefixAndIsStable) {
    auto m = gpus_with_nvlinks(2, {{0, 1}});
    EXPECT_EQ(topo1(m).rfind("topo1:sha256:", 0), 0u);
    EXPECT_EQ(topo1(m), topo1(m));
}

TEST(Identity, EqualAcrossRenumberingAndPermutations) { // RFC-0003 Testing
    auto ring8 = gpus_with_nvlinks(8, {{0, 1},
                                       {1, 2},
                                       {2, 3},
                                       {3, 4},
                                       {4, 5},
                                       {5, 6},
                                       {6, 7},
                                       {7, 0},
                                       {0, 4},
                                       {1, 5},
                                       {2, 6},
                                       {3, 7}});
    for (unsigned seed = 1; seed <= 20; ++seed)
        EXPECT_EQ(topo1(ring8), topo1(renumbered(ring8, seed))) << seed;
}

TEST(Identity, CycleOfSixDiffersFromTwoTriangles) { // a WL collision (RFC-0003 §5)
    auto cycle = gpus_with_nvlinks(6, {{0, 1}, {1, 2}, {2, 3}, {3, 4}, {4, 5}, {5, 0}});
    auto triangles = gpus_with_nvlinks(6, {{0, 1}, {1, 2}, {2, 0}, {3, 4}, {4, 5}, {5, 3}});
    EXPECT_NE(topo1(cycle), topo1(triangles));
}

TEST(Identity, DiffersWhenAnNvlinkIsRemovedOrTheModelChanges) {
    auto m = gpus_with_nvlinks(4, {{0, 1}, {1, 2}, {2, 3}, {3, 0}});
    auto fewer = gpus_with_nvlinks(4, {{0, 1}, {1, 2}, {2, 3}});
    auto other = m;
    other.nodes[0].attrs["model"] = std::string("A100");
    EXPECT_NE(topo1(m), topo1(fewer));
    EXPECT_NE(topo1(m), topo1(other));
}

TEST(Identity, IgnoresDataAttributes) { // RFC-0003 §5
    Model a, b;
    a.nodes = {{NodeKind::nic,
                "0000:3b:00.0",
                {{"pci_vendor", 0x15b3},
                 {"pci_device", 0x101b},
                 {"link_layer", std::string("unknown")},
                 {"rdma_probe", std::string("unavailable")}}}};
    b.nodes = {{NodeKind::nic,
                "0000:5e:00.0",
                {{"pci_vendor", 0x15b3},
                 {"pci_device", 0x101b},
                 {"link_layer", std::string("infiniband")},
                 {"port_speed_mbps", 200000},
                 {"rdma_probe", std::string("ok")}}}};
    EXPECT_EQ(topo1(a), topo1(b));
}

// CPU time, not wall time: on a contended runner wall time measures the neighbours.
static double cpu_seconds() { return double(std::clock()) / CLOCKS_PER_SEC; }

// Sanitizer builds run several times slower (RFC-0003 Performance).
#ifdef OSTIA_TOPO_SANITIZED
static constexpr double kLimitSeconds = 5.0;
#else
static constexpr double kLimitSeconds = 1.0;
#endif

TEST(Identity, SymmetricEightGpuSwitchIsFast) { // twin pruning (RFC-0003 §5)
    Model m = gpus_with_nvlinks(8, {});
    m.nodes.push_back({NodeKind::switch_group, "switch-group-0", {}});
    for (auto& n : std::vector<Node>(m.nodes.begin(), m.nodes.end() - 1))
        m.edges.push_back({EdgeKind::nvlink, n.key, "switch-group-0", {{"links", 18}}});
    const double t0 = cpu_seconds();
    auto id = topo1(m);
    const double seconds = cpu_seconds() - t0;
    EXPECT_LT(seconds, kLimitSeconds) << seconds << " s of CPU time";
    EXPECT_EQ(id, topo1(renumbered(m, 3)));
}

// g GPU and k NIC branches, each under its own host bridge, over u NUMA nodes; the GPUs share an
// NVLink switch group. No two vertices are twins, so only automorphism pruning keeps the search
// small (RFC-0003 Performance).
static Model branches(int g, int k, int numas) {
    Model m;
    auto numa = [](int u) { return "numa-" + std::to_string(u); };
    for (int u = 0; u < numas; ++u)
        m.nodes.push_back({NodeKind::numa, numa(u), {}});
    m.nodes.push_back({NodeKind::switch_group, "switch-group-0", {}});
    auto branch = [&](bool is_gpu, const std::string& key, int u) {
        m.nodes.push_back({NodeKind::pcie_bridge, "hostbridge-" + key, {}});
        if (is_gpu)
            m.nodes.push_back({NodeKind::gpu,
                               key,
                               {{"model", std::string("H100")}, {"cc_major", 9}, {"cc_minor", 0}}});
        else
            m.nodes.push_back(
                {NodeKind::nic, key, {{"pci_vendor", 0x15b3}, {"pci_device", 0x1021}}});
        m.edges.push_back({EdgeKind::pcie, "hostbridge-" + key, key, {{"gen", 5}, {"width", 16}}});
        m.edges.push_back({EdgeKind::numa_local, key, numa(u), {}});
        if (is_gpu)
            m.edges.push_back({EdgeKind::nvlink, key, "switch-group-0", {{"links", 18}}});
    };
    for (int i = 0; i < g; ++i)
        branch(true, "gpu-" + std::to_string(i), i % numas);
    for (int i = 0; i < k; ++i)
        branch(false, "nic-" + std::to_string(i), i % numas);
    return m;
}

struct Shape {
    int gpus, nics, numas;
};
static constexpr Shape kBranchShapes[] = {{8, 8, 1}, {8, 8, 2}, {16, 0, 1}};

TEST(Identity, NonTwinBranchesAreFast) { // RFC-0003 Performance
    for (const Shape& s : kBranchShapes) {
        Model m = branches(s.gpus, s.nics, s.numas);
        const double t0 = cpu_seconds();
        EXPECT_NO_THROW(topo1(m)) << s.gpus << "+" << s.nics << "/" << s.numas; // no leaf_cap
        const double seconds = cpu_seconds() - t0;
        EXPECT_LT(seconds, kLimitSeconds)
            << s.gpus << "+" << s.nics << "/" << s.numas << ": " << seconds << " s of CPU time";
    }
}

TEST(Identity, SymmetricShapesAreInvariantUnderRenumbering) { // RFC-0003 Testing
    for (const Shape& s : kBranchShapes) {
        Model m = branches(s.gpus, s.nics, s.numas);
        const std::string id = topo1(m);
        for (unsigned seed = 1; seed <= 3; ++seed)
            EXPECT_EQ(id, topo1(renumbered(m, seed))) << s.gpus << "+" << s.nics << "/" << s.numas;
    }
}

TEST(Identity, NodeCapIsATypedError) {
    try {
        topo1(gpus_with_nvlinks(257, {}));
        FAIL();
    } catch (const TopologyError& e) {
        EXPECT_EQ(e.code, "node_cap");
    }
}

TEST(Identity, PairIdIsOrderIndependentAndSeesRails) {
    nlohmann::json pair = {
        {"link_class", "infiniband"},
        {"rails", {{{"node-0", {{"nic_index", 0}}}, {"node-1", {{"nic_index", 0}}}}}}};
    auto p = pair_id("topo1:sha256:aa", "topo1:sha256:bb", pair);
    EXPECT_EQ(p, pair_id("topo1:sha256:bb", "topo1:sha256:aa", pair));
    pair["rails"][0]["node-1"]["nic_index"] = 1;
    EXPECT_NE(p, pair_id("topo1:sha256:aa", "topo1:sha256:bb", pair));
    EXPECT_EQ(p.rfind("topo1:sha256:", 0), 0u);
}

TEST(Identity, PairIdKeepsRailEndpointsWithTheirNode) { // RFC-0003 §7
    nlohmann::json r = {
        {"link_class", "infiniband"},
        {"rails", {{{"node-0", {{"nic_index", 0}}}, {"node-1", {{"nic_index", 1}}}}}}};
    nlohmann::json r_swapped = {
        {"link_class", "infiniband"},
        {"rails", {{{"node-0", {{"nic_index", 1}}}, {"node-1", {{"nic_index", 0}}}}}}};
    const std::string a = "topo1:sha256:aa", b = "topo1:sha256:bb";
    EXPECT_EQ(pair_id(a, b, r), pair_id(b, a, r_swapped));
    EXPECT_NE(pair_id(a, b, r), pair_id(b, a, r));
}

TEST(Identity, PairIdWithEqualIdsIgnoresOrientation) { // RFC-0003 §7
    nlohmann::json r = {
        {"link_class", "infiniband"},
        {"rails", {{{"node-0", {{"nic_index", 0}}}, {"node-1", {{"nic_index", 1}}}}}}};
    nlohmann::json r_swapped = {
        {"link_class", "infiniband"},
        {"rails", {{{"node-0", {{"nic_index", 1}}}, {"node-1", {{"nic_index", 0}}}}}}};
    const std::string a = "topo1:sha256:aa";
    EXPECT_EQ(pair_id(a, a, r), pair_id(a, a, r_swapped));
}
