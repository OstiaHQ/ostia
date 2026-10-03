#include <cstdint>
#include <gtest/gtest.h>
#include <string>
#include <utility>
#include <vector>

#include "topology/identity.hpp"

using namespace ostia::fabric::topology;

// These literals pin topo1 across changes to the canonical-labelling search: any id change breaks
// every consumer's stored ids (RFC-0003 §5), so a pruning change must reproduce them exactly.

static std::string name(const char* prefix, int i) {
    return std::string(prefix) + std::to_string(i);
}

static Node gpu(std::string key) {
    return {NodeKind::gpu,
            std::move(key),
            {{"model", std::string("H100")}, {"cc_major", 9}, {"cc_minor", 0}}};
}

static Node nic(std::string key) {
    return {NodeKind::nic, std::move(key), {{"pci_vendor", 0x15b3}, {"pci_device", 0x1021}}};
}

static Node plain(NodeKind kind, std::string key) { return {kind, std::move(key), {}}; }

static Edge nvlink(std::string a, std::string b, int links = 2) {
    return {EdgeKind::nvlink, std::move(a), std::move(b), {{"links", links}}};
}

static Edge pcie(std::string a, std::string b, int gen = 5, int width = 16) {
    return {EdgeKind::pcie, std::move(a), std::move(b), {{"gen", gen}, {"width", width}}};
}

static Edge numa_local(std::string a, std::string b) {
    return {EdgeKind::numa_local, std::move(a), std::move(b), {}};
}

// g GPU and k NIC branches, each under its own host bridge; the GPUs share one switch group.
static Model branches(int g, int k, int numas) {
    Model m;
    m.nodes.push_back(plain(NodeKind::package, "package-0"));
    for (int u = 0; u < numas; ++u)
        m.nodes.push_back(plain(NodeKind::numa, name("numa-", u)));
    if (g > 0)
        m.nodes.push_back(plain(NodeKind::switch_group, "switch-group-0"));
    auto branch = [&](bool is_gpu, std::string key, int u) {
        std::string hb = "hostbridge-" + key;
        m.nodes.push_back(plain(NodeKind::pcie_bridge, hb));
        m.nodes.push_back(is_gpu ? gpu(key) : nic(key));
        m.edges.push_back(pcie(hb, key));
        m.edges.push_back(numa_local(key, name("numa-", u)));
        if (is_gpu)
            m.edges.push_back(nvlink(key, "switch-group-0", 18));
    };
    for (int i = 0; i < g; ++i)
        branch(true, name("g", i), i % numas);
    for (int i = 0; i < k; ++i)
        branch(false, name("n", i), i % numas);
    return m;
}

static Model gpu_graph(int n, const std::vector<std::pair<int, int>>& links) {
    Model m;
    for (int i = 0; i < n; ++i)
        m.nodes.push_back(gpu(name("g", i)));
    for (auto [a, b] : links)
        m.edges.push_back(nvlink(name("g", a), name("g", b)));
    return m;
}

static Model cycle6() { return gpu_graph(6, {{0, 1}, {1, 2}, {2, 3}, {3, 4}, {4, 5}, {5, 0}}); }

static Model two_triangles() {
    return gpu_graph(6, {{0, 1}, {1, 2}, {2, 0}, {3, 4}, {4, 5}, {5, 3}});
}

// DGX-1 hybrid cube-mesh: two quads joined by a perfect matching.
static Model dgx1() {
    return gpu_graph(8, {{0, 1},
                         {0, 2},
                         {0, 3},
                         {1, 2},
                         {1, 3},
                         {2, 3},
                         {4, 5},
                         {4, 6},
                         {4, 7},
                         {5, 6},
                         {5, 7},
                         {6, 7},
                         {0, 4},
                         {1, 5},
                         {2, 6},
                         {3, 7}});
}

static Model grid_4x2() {
    std::vector<std::pair<int, int>> links;
    for (int r = 0; r < 2; ++r)
        for (int c = 0; c < 4; ++c) {
            int v = r * 4 + c;
            if (c < 3)
                links.push_back({v, v + 1});
            if (r < 1)
                links.push_back({v, v + 4});
        }
    return gpu_graph(8, links);
}

static Model cube() {
    std::vector<std::pair<int, int>> links;
    for (int v = 0; v < 8; ++v)
        for (int bit = 1; bit < 8; bit <<= 1)
            if (!(v & bit))
                links.push_back({v, v | bit});
    return gpu_graph(8, links);
}

// Generalised Petersen graph GP(n, k): an outer n-cycle, n spokes, an inner star polygon {n/k}.
static Model generalised_petersen(int n, int k) {
    std::vector<std::pair<int, int>> links;
    for (int i = 0; i < n; ++i) {
        links.push_back({i, (i + 1) % n});
        links.push_back({i, n + i});
        links.push_back({n + i, n + (i + k) % n});
    }
    return gpu_graph(2 * n, links);
}

// PCIe is directed: a root feeds a bridge chain, with identical leaves on each tier.
static Model pcie_chain(int depth, int leaves) {
    Model m;
    m.nodes.push_back(plain(NodeKind::package, "package-0"));
    std::string up = "package-0";
    for (int d = 0; d < depth; ++d) {
        std::string b = name("bridge-", d);
        m.nodes.push_back(plain(NodeKind::pcie_bridge, b));
        m.edges.push_back(pcie(up, b, 5, 16));
        for (int l = 0; l < leaves; ++l) {
            std::string leaf = "gpu-" + std::to_string(d) + "-" + std::to_string(l);
            m.nodes.push_back(gpu(leaf));
            m.edges.push_back(pcie(b, leaf, 5, 16));
        }
        up = b;
    }
    return m;
}

// numa_local is directed: many devices point at few NUMA nodes.
static Model numa_fan_in(int gpus, int nics, int numas) {
    Model m;
    for (int u = 0; u < numas; ++u)
        m.nodes.push_back(plain(NodeKind::numa, name("numa-", u)));
    for (int i = 0; i < gpus; ++i) {
        m.nodes.push_back(gpu(name("g", i)));
        m.edges.push_back(numa_local(name("g", i), name("numa-", i % numas)));
    }
    for (int i = 0; i < nics; ++i) {
        m.nodes.push_back(nic(name("n", i)));
        m.edges.push_back(numa_local(name("n", i), name("numa-", i % numas)));
    }
    return m;
}

// 32-bit LCG with fixed seeds: <random> cannot be parsed by `ostia-dev check macros`.
static Model random_model(std::uint32_t seed) {
    std::uint32_t x = seed;
    auto next = [&](std::uint32_t bound) {
        x = x * 1664525u + 1013904223u;
        return (x >> 16) % bound;
    };
    const NodeKind kinds[] = {NodeKind::pcie_bridge, NodeKind::gpu, NodeKind::nic, NodeKind::numa};
    int n = 6 + int(next(5));
    Model m;
    std::vector<NodeKind> kind_of;
    for (int i = 0; i < n; ++i) {
        NodeKind k = kinds[next(4)];
        kind_of.push_back(k);
        std::string key = name("v", i);
        if (k == NodeKind::gpu) {
            Node g = gpu(key);
            g.attrs["cc_minor"] = std::int64_t(next(2));
            m.nodes.push_back(g);
        } else if (k == NodeKind::nic) {
            Node q = nic(key);
            q.attrs["pci_device"] = std::int64_t(0x1000 + next(2));
            m.nodes.push_back(q);
        } else {
            m.nodes.push_back(plain(k, key));
        }
    }
    int edges = n + int(next(n));
    for (int e = 0; e < edges; ++e) {
        int a = int(next(n)), b = int(next(n));
        if (a == b)
            continue;
        std::string ka = name("v", a), kb = name("v", b);
        bool dup = false;
        for (auto& old : m.edges)
            dup = dup || (old.from == ka && old.to == kb) || (old.from == kb && old.to == ka);
        if (dup)
            continue;
        switch (next(3)) {
        case 0:
            m.edges.push_back(pcie(ka, kb, 4 + int(next(2)), next(2) ? 8 : 16));
            break;
        case 1:
            m.edges.push_back(nvlink(ka, kb, 1 + int(next(3))));
            break;
        default:
            m.edges.push_back(numa_local(ka, kb));
        }
    }
    return m;
}

TEST(IdentityCorpus, Branches_4gpu_0nic_1numa) {
    EXPECT_EQ(topo1(branches(4, 0, 1)),
              "topo1:sha256:d92e944a5dc60a9ed627cb4b5140978397692e693f43ed2d90de9f7a6110f1bd");
}

TEST(IdentityCorpus, Branches_6gpu_0nic_1numa) {
    EXPECT_EQ(topo1(branches(6, 0, 1)),
              "topo1:sha256:18c44717fa87aa8fbdbab85a9d027ab4a062808bfaa2c667adcaf87a9f83f413");
}

TEST(IdentityCorpus, Branches_4gpu_4nic_1numa) {
    EXPECT_EQ(topo1(branches(4, 4, 1)),
              "topo1:sha256:0eddbfbc690e1f596a232206ea981754ed454ffb64a5f164e2bee102aef273f4");
}

TEST(IdentityCorpus, Branches_6gpu_6nic_2numa) {
    EXPECT_EQ(topo1(branches(6, 6, 2)),
              "topo1:sha256:dc746576e773121fae04f4624cdfbe3c75a4e27164d1be1aa8f50b5e57d9b33b");
}

TEST(IdentityCorpus, Branches_3gpu_3nic_1numa) {
    EXPECT_EQ(topo1(branches(3, 3, 1)),
              "topo1:sha256:cb7de98c9017d52877766b2a6931d5f42fe4b93ff82debf31a4ba520df3b14df");
}

TEST(IdentityCorpus, Branches_4gpu_4nic_2numa) {
    EXPECT_EQ(topo1(branches(4, 4, 2)),
              "topo1:sha256:b0546b863564bfdd8f8a71fdf6fe599409b9ba396112e27f18b8ad77bb7f7e16");
}

TEST(IdentityCorpus, Branches_2gpu_2nic_1numa) {
    EXPECT_EQ(topo1(branches(2, 2, 1)),
              "topo1:sha256:7d382f03bf6cb33942fb8950f827cb0ee16ab85c4a613d5964319a253262faa9");
}

TEST(IdentityCorpus, Cycle6) {
    EXPECT_EQ(topo1(cycle6()),
              "topo1:sha256:aec2aa320247e5f25e7c9ceab98d23faef1f8060dcd01823cbec9f963e2f812f");
}

TEST(IdentityCorpus, TwoTriangles) {
    EXPECT_EQ(topo1(two_triangles()),
              "topo1:sha256:7c2238c939ad92bce1196a752ff9b187db229e3350f39354f090252847032b6f");
}

TEST(IdentityCorpus, Dgx1CubeMesh) {
    EXPECT_EQ(topo1(dgx1()),
              "topo1:sha256:461aedae25e24c8d62d74eb0c793927df24e24a64050d634fd6edf7c956b44b7");
}

TEST(IdentityCorpus, Grid4x2) {
    EXPECT_EQ(topo1(grid_4x2()),
              "topo1:sha256:a8abb4e3576943a5601ce908ed35621f530abe87495a042e6d1535105c394eee");
}

TEST(IdentityCorpus, Cube) {
    EXPECT_EQ(topo1(cube()),
              "topo1:sha256:e38ceb71fd370cf7dacd2b212b2f0681bc00a317379f3ca67c8350084a0427ba");
}

TEST(IdentityCorpus, MobiusKantorLike) {
    EXPECT_EQ(topo1(generalised_petersen(8, 3)),
              "topo1:sha256:64d8bd574b4639ae5af23f90b8a59b23b2d8bad42ac29516ded27c4f67c0eaef");
}

TEST(IdentityCorpus, Petersen) {
    EXPECT_EQ(topo1(generalised_petersen(5, 2)),
              "topo1:sha256:6e37d9e1c32165447010f4a10764886e2164be344cef595d6c3f1f2bb7f2ddad");
}

TEST(IdentityCorpus, PcieChainDepth3Leaves3) {
    EXPECT_EQ(topo1(pcie_chain(3, 3)),
              "topo1:sha256:e1b19a3472a58b354a78f4c6922bb35483a9ee942165249a10cc4a43ae74ef82");
}

TEST(IdentityCorpus, PcieChainDepth2Leaves4) {
    EXPECT_EQ(topo1(pcie_chain(2, 4)),
              "topo1:sha256:3471ba91d5b1123586a60e63ba4eba22888d12183ceb167a0f8b8edbb44d2231");
}

TEST(IdentityCorpus, NumaFanIn6Gpu6Nic2Numa) {
    EXPECT_EQ(topo1(numa_fan_in(6, 6, 2)),
              "topo1:sha256:45ba7c110eab57f249e40fa42ebb9bca3818cd4d243defdf5874103bf949bb54");
}

TEST(IdentityCorpus, NumaFanIn8Gpu1Numa) {
    EXPECT_EQ(topo1(numa_fan_in(8, 0, 1)),
              "topo1:sha256:16feff16496e747ff44fc827176c61ac1fc9dfc9cc4fbc66a106fbf0a4371054");
}

TEST(IdentityCorpus, Random_seed1) {
    EXPECT_EQ(topo1(random_model(1u)),
              "topo1:sha256:71928a541e3364edd97265e6058a10b5605ee01c5cf7d8cfd6edd037434f0b60");
}

TEST(IdentityCorpus, Random_seed2) {
    EXPECT_EQ(topo1(random_model(2u)),
              "topo1:sha256:e9248d1b7242a475faa2733439ce72874a84f58d7dffcbcbc262a343e2131766");
}

TEST(IdentityCorpus, Random_seed3) {
    EXPECT_EQ(topo1(random_model(3u)),
              "topo1:sha256:94f943b9d3dec0ee2bf53a5a486d1be1c10734f7c63137a867f1c61c08bfcd87");
}

TEST(IdentityCorpus, Random_seed4) {
    EXPECT_EQ(topo1(random_model(4u)),
              "topo1:sha256:8bb4735483a932762d345246b816144b47c67fffba008a949e21025cf8d995e7");
}

TEST(IdentityCorpus, Random_seed5) {
    EXPECT_EQ(topo1(random_model(5u)),
              "topo1:sha256:182c9ed1a84be47cdf0aeac468e18740142826aa91bbf4fb78d4a3734785a6e5");
}

TEST(IdentityCorpus, Random_seed6) {
    EXPECT_EQ(topo1(random_model(6u)),
              "topo1:sha256:5b483addd86086bd4cf7b9456588998acbf351385b720b7d6ab278f7a2710f23");
}

TEST(IdentityCorpus, Random_seed7) {
    EXPECT_EQ(topo1(random_model(7u)),
              "topo1:sha256:bb2bbf13779f9c4115136ed2bb8b01cf3c0c338e6977508157b90c5a6e56e1eb");
}

TEST(IdentityCorpus, Random_seed8) {
    EXPECT_EQ(topo1(random_model(8u)),
              "topo1:sha256:f56fd6e906bd7bbda0cee7b02fea06dfd37d06a43731377985e2a466772a090e");
}

TEST(IdentityCorpus, Random_seed9) {
    EXPECT_EQ(topo1(random_model(9u)),
              "topo1:sha256:d60be54a9b98e4d883c2dc7fb671b309a5c4733909a3a8034e4a17483ef0f1b8");
}

TEST(IdentityCorpus, Random_seed10) {
    EXPECT_EQ(topo1(random_model(10u)),
              "topo1:sha256:59b7209eab7dddc3b99df95bbb207ac01e3bc9c98224f506494ec57ad670483c");
}
