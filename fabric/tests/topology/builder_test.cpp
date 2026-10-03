#include <algorithm>
#include <gtest/gtest.h>
#include <string>
#include <vector>

#include "topology/builder.hpp"
#include "topology/error.hpp"
using namespace ostia::fabric::topology;
using nlohmann::json;

static Facts two_gpus_one_nic() {
    Facts f;
    f.packages = 1;
    f.numa_nodes = {0};
    f.pci = {
        {"", "hostbridge-0000:00", "", true, 0x0600, 0, 0, 0, 0, {0}},
        {"0000:07:00.0",
         "0000:07:00.0",
         "hostbridge-0000:00",
         false,
         0x0302,
         0x10de,
         0x20b2,
         4,
         16,
         {0}},
        {"0000:0a:00.0",
         "0000:0a:00.0",
         "hostbridge-0000:00",
         false,
         0x0302,
         0x10de,
         0x20b2,
         4,
         16,
         {0}},
        {"0000:3b:00.0",
         "0000:3b:00.0",
         "hostbridge-0000:00",
         false,
         0x0207,
         0x15b3,
         0x101b,
         4,
         16,
         {0}},
        {"0000:00:1f.0",
         "0000:00:1f.0",
         "hostbridge-0000:00",
         false,
         0x0601,
         0x8086,
         0x1234,
         3,
         1,
         {0}},
    };
    auto gpu = [](const char* bus, const char* peer, int links) {
        json g = {{"bus_id", bus},           {"name", "NVIDIA A100-SXM4-80GB"}, {"cc_major", 8},
                  {"cc_minor", 0},           {"memory_bytes", 85899345920},     {"cuda_ordinal", 0},
                  {"nvlinks", json::array()}};
        for (int i = 0; i < links; ++i)
            g["nvlinks"].push_back({{"link", i},
                                    {"state", "active"},
                                    {"version", 4},
                                    {"remote_type", "gpu"},
                                    {"remote_bus_id", peer}});
        return g;
    };
    f.nvml = {
        {"schema", 1},
        {"gpus", {gpu("0000:07:00.0", "0000:0a:00.0", 4), gpu("0000:0a:00.0", "0000:07:00.0", 4)}},
        {"p2p", json::array()}};
    f.nics = json::parse(
        R"({"schema":1,"rdma_probe":"unavailable","rdma":[],"nics":[{"bus_id":"0000:3b:00.0",
      "driver":"mlx5_core","link_layer":"unknown","max_pcie_gen":4,"max_pcie_width":16,"numa_node":0,
      "port_speed_mbps":"unknown"}]})");
    return f;
}

TEST(Builder, BuildsGpusNicsBridgeAndDropsOtherDevices) {
    auto j = to_json(build(two_gpus_one_nic()));
    std::vector<std::string> kinds;
    for (auto& n : j["nodes"])
        kinds.push_back(n["kind"]);
    EXPECT_EQ(kinds,
              (std::vector<std::string>{"gpu", "gpu", "nic", "pcie_bridge", "numa", "package"}));
}

TEST(Builder, NvlinkPairIsOneEdgeWithLinkCount) {
    auto m = build(two_gpus_one_nic());
    int nvlink = 0;
    for (auto& e : m.edges)
        if (e.kind == EdgeKind::nvlink) {
            ++nvlink;
            EXPECT_EQ(e.attrs.at("links"), 4);
        }
    EXPECT_EQ(nvlink, 1);
}

TEST(Builder, InactiveLinksAreNotCounted) { // R7, broken-nvlink
    auto f = two_gpus_one_nic();
    f.nvml["gpus"][0]["nvlinks"][0]["state"] = "inactive";
    f.nvml["gpus"][1]["nvlinks"][0]["state"] = "inactive";
    for (auto& e : build(f).edges)
        if (e.kind == EdgeKind::nvlink)
            EXPECT_EQ(e.attrs.at("links"), 3);
}

TEST(Builder, SwitchLinksFormOneSwitchGroup) { // R7, nvswitch-hidden
    auto f = two_gpus_one_nic();
    for (auto& g : f.nvml["gpus"])
        for (auto& l : g["nvlinks"]) {
            l["remote_type"] = "switch";
            l.erase("remote_bus_id");
        }
    auto j = to_json(build(f));
    EXPECT_EQ(std::count_if(j["nodes"].begin(), j["nodes"].end(),
                            [](auto& n) { return n["key"] == "switch-group-0"; }),
              1);
}

TEST(Builder, UnknownPortFactsAreRecordedAsUnknown) { // D4, R8
    for (auto& n : to_json(build(two_gpus_one_nic()))["nodes"])
        if (n["kind"] == "nic") {
            EXPECT_EQ(n["port_speed_mbps"], "unknown");
            EXPECT_EQ(n["link_layer"], "unknown");
        }
}

TEST(Builder, NotSupportedNvlinksGiveNoNvlinkEdges) { // partial-discovery
    auto f = two_gpus_one_nic();
    for (auto& g : f.nvml["gpus"])
        g["nvlinks"] = "not_supported";
    for (auto& e : build(f).edges)
        EXPECT_NE(e.kind, EdgeKind::nvlink);
}

TEST(Builder, DanglingGpuBusIdNamesFileAndBusId) { // Review Focus 2
    auto f = two_gpus_one_nic();
    f.nvml["gpus"][1]["bus_id"] = "0000:99:00.0";
    try {
        build(f);
        FAIL();
    } catch (const TopologyError& e) {
        EXPECT_EQ(e.code, "dangling_reference");
        EXPECT_EQ(e.file, "nvml.json");
        EXPECT_NE(std::string(e.what()).find("0000:99:00.0"), std::string::npos);
    }
}

TEST(Builder, OneSidedInactiveLinkReducesCount) { // an NVLink needs both ends active
    auto f = two_gpus_one_nic();
    f.nvml["gpus"][0]["nvlinks"][0]["state"] = "inactive";
    int seen = 0;
    for (auto& e : build(f).edges)
        if (e.kind == EdgeKind::nvlink) {
            ++seen;
            EXPECT_EQ(e.attrs.at("links"), 3);
        }
    EXPECT_EQ(seen, 1);
}
