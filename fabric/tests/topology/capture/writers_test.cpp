#include <cstdint>
#include <gtest/gtest.h>
#include <nlohmann/json.hpp>
#include <optional>
#include <string>
#include <vector>

#include "core/apis.hpp"
#include "core/nics.hpp"
#include "core/result.hpp"
#include "core/writers.hpp"
#include "topology/schema.hpp"

using ostia::fabric::topology::validate;
using ostia::fabric::topology::capture::dump;
using ostia::fabric::topology::capture::meta_json;
using ostia::fabric::topology::capture::MetaFlags;
using ostia::fabric::topology::capture::NicFacts;
using ostia::fabric::topology::capture::nics_json;
using ostia::fabric::topology::capture::normalize_bus_id;
using ostia::fabric::topology::capture::NotSupported;
using ostia::fabric::topology::capture::NvLink;
using ostia::fabric::topology::capture::nvml_json;
using ostia::fabric::topology::capture::NvmlFacts;
using ostia::fabric::topology::capture::NvmlGpu;
using ostia::fabric::topology::capture::P2p;
using ostia::fabric::topology::capture::Result;
using ostia::fabric::topology::capture::VerbsPort;

namespace {

using nlohmann::json;

constexpr std::int64_t kGib = std::int64_t{1} << 30;

NvmlGpu gpu(const std::string& bus_id) {
    // uuid, serial and board_id are raw leak-check inputs that no writer may emit.
    return NvmlGpu{.bus_id = bus_id,
                   .name = "OstiaFakeGPU",
                   .cc_major = 8,
                   .cc_minor = 9,
                   .memory_bytes = 24 * kGib,
                   .nvlinks = NotSupported{},
                   .uuid = "GPU-0a1b2c3d-0000-4000-8000-0123456789ab",
                   .serial = "OSTIA-GPU-SERIAL-0001",
                   .board_id = "OSTIA-BOARD-0001"};
}

NvmlFacts three_gpus_out_of_order() {
    NvmlFacts facts;
    NvmlGpu b = gpu("0000:3b:00.0");
    b.nvlinks = std::vector<NvLink>{NvLink{.link = 0,
                                           .state = "active",
                                           .version = 4,
                                           .remote_type = "gpu",
                                           .remote_bus_id = "0000:1a:00.0"},
                                    NvLink{.link = 1,
                                           .state = "inactive",
                                           .version = 4,
                                           .remote_type = "switch",
                                           .remote_bus_id = std::nullopt}};
    facts.gpus = {b, gpu("0000:1a:00.0"), gpu("0000:5e:00.0")};
    facts.p2p = {P2p{.a = "0000:3b:00.0",
                     .b = "0000:5e:00.0",
                     .read = "ok",
                     .write = "ok",
                     .nvlink = "not_supported",
                     .atomics = "unknown"},
                 P2p{.a = "0000:1a:00.0",
                     .b = "0000:3b:00.0",
                     .read = "ok",
                     .write = "ok",
                     .nvlink = "ok",
                     .atomics = "ok"}};
    facts.driver = "550.54.15";
    facts.cuda = "12.4";
    return facts;
}

bool valid(const char* schema, const json& doc) { return validate(schema, doc).empty(); }

} // namespace

TEST(NormalizeBusId, LowersAndShortensTheDomain) {
    const Result<std::string> id = normalize_bus_id("00000000:3B:00.0");
    ASSERT_TRUE(id.ok());
    EXPECT_EQ(id.value(), "0000:3b:00.0");
    EXPECT_EQ(normalize_bus_id("0000:3b:1f.7").value(), "0000:3b:1f.7");
    EXPECT_EQ(normalize_bus_id("0001:00:00.0").value(), "0001:00:00.0");
}

TEST(NormalizeBusId, RefusesAWideDomainAndMalformedIds) {
    const Result<std::string> wide = normalize_bus_id("00010000:3b:00.0");
    ASSERT_FALSE(wide.ok());
    EXPECT_EQ(wide.error(), "bus_id_domain");
    EXPECT_EQ(normalize_bus_id("0000:3b:00").error(), "bus_id_format");
    EXPECT_FALSE(normalize_bus_id("").ok());
    EXPECT_FALSE(normalize_bus_id("0000:3b:00").ok());
    EXPECT_FALSE(normalize_bus_id("0000:3b:00.8").ok());
    EXPECT_FALSE(normalize_bus_id("0000:3g:00.0").ok());
    EXPECT_FALSE(normalize_bus_id("0000:3b:00.0 ").ok());
}

TEST(NvmlJson, OrdinalsFollowBusIdOrder) {
    const json doc = nvml_json(three_gpus_out_of_order());
    ASSERT_TRUE(valid("nvml", doc));
    ASSERT_EQ(doc["gpus"].size(), 3U);
    EXPECT_EQ(doc["gpus"][0]["bus_id"], "0000:1a:00.0");
    EXPECT_EQ(doc["gpus"][0]["cuda_ordinal"], 0);
    EXPECT_EQ(doc["gpus"][1]["bus_id"], "0000:3b:00.0");
    EXPECT_EQ(doc["gpus"][1]["cuda_ordinal"], 1);
    EXPECT_EQ(doc["gpus"][2]["cuda_ordinal"], 2);
    EXPECT_EQ(doc["p2p"][0]["a"], "0000:1a:00.0");
}

TEST(NvmlJson, WritesLinksAndNotSupported) {
    const json doc = nvml_json(three_gpus_out_of_order());
    EXPECT_EQ(doc["gpus"][0]["nvlinks"], "not_supported");
    const json& links = doc["gpus"][1]["nvlinks"];
    ASSERT_EQ(links.size(), 2U);
    EXPECT_EQ(links[0], json({{"link", 0},
                              {"state", "active"},
                              {"version", 4},
                              {"remote_type", "gpu"},
                              {"remote_bus_id", "0000:1a:00.0"}}));
    EXPECT_FALSE(links[1].contains("remote_bus_id"));
    EXPECT_EQ(doc["gpus"][1]["memory_bytes"], 24 * kGib);
    EXPECT_TRUE(json::parse(dump(doc)) == doc);
}

TEST(NvmlJson, DropsUuidSerialAndBoardId) {
    const NvmlFacts facts = three_gpus_out_of_order();
    const std::string text = dump(nvml_json(facts));
    EXPECT_EQ(text.find("uuid"), std::string::npos);
    EXPECT_EQ(text.find("serial"), std::string::npos);
    EXPECT_EQ(text.find("board"), std::string::npos);
    const NvmlGpu& g = facts.gpus[0];
    EXPECT_EQ(text.find(g.uuid), std::string::npos) << "uuid value present";
    EXPECT_EQ(text.find(g.serial), std::string::npos) << "serial value present";
    EXPECT_EQ(text.find(g.board_id), std::string::npos) << "board id value present";
}

TEST(NicsJson, UnavailableProbeHasNoRdma) {
    const std::vector<NicFacts> nics{
        NicFacts{.bus_id = "0000:11:01.0",
                 .driver = "mlx5_core",
                 .link_layer = "ethernet",
                 .max_gen = 4,
                 .max_width = 16,
                 .numa = 0,
                 .port_speed_mbps = 100000},
    };
    const json doc = nics_json(nics, std::nullopt);
    ASSERT_TRUE(valid("nics", doc));
    EXPECT_EQ(doc["rdma_probe"], "unavailable");
    EXPECT_EQ(doc["rdma"], json::array());
    EXPECT_EQ(doc["nics"][0]["port_speed_mbps"], 100000);
    EXPECT_EQ(doc["nics"][0]["max_pcie_gen"], 4);
    EXPECT_EQ(doc["nics"][0]["numa_node"], 0);
}

TEST(NicsJson, EmptyProbeIsOk) {
    const json doc = nics_json({}, std::vector<VerbsPort>{});
    ASSERT_TRUE(valid("nics", doc));
    EXPECT_EQ(doc["rdma_probe"], "ok");
    EXPECT_EQ(doc["nics"], json::array());
    EXPECT_EQ(doc["rdma"], json::array());
}

TEST(NicsJson, WritesVerbsPortsWithGpudirect) {
    const std::vector<NicFacts> nics{
        NicFacts{.bus_id = "0000:12:00.0",
                 .driver = "mlx5_core",
                 .link_layer = "infiniband",
                 .max_gen = 5,
                 .max_width = 16,
                 .numa = -1,
                 .port_speed_mbps = std::string("unknown")},
    };
    const std::vector<VerbsPort> ports{
        VerbsPort{.device = "mlx5_1",
                  .bus_id = "0000:12:00.0",
                  .state = "ACTIVE",
                  .link_layer = "InfiniBand",
                  .gpudirect = "nvidia_peermem",
                  .port = 1,
                  .active_speed = 128,
                  .active_width = 2},
    };
    const json doc = nics_json(nics, ports);
    ASSERT_TRUE(valid("nics", doc));
    EXPECT_EQ(doc["rdma_probe"], "ok");
    EXPECT_EQ(doc["nics"][0]["port_speed_mbps"], "unknown");
    EXPECT_EQ(doc["nics"][0]["numa_node"], -1);
    ASSERT_EQ(doc["rdma"].size(), 1U);
    EXPECT_EQ(doc["rdma"][0]["gpudirect"], "nvidia_peermem");
    EXPECT_EQ(doc["rdma"][0]["link_layer"], "infiniband");
    EXPECT_EQ(doc["rdma"][0]["active_speed"], 128);
    EXPECT_EQ(doc["rdma"][0]["port"], 1);
}

TEST(MetaJson, UnknownVersionsWithoutNvml) {
    const json doc =
        meta_json(MetaFlags{.provider = "runpod", .instance_type = "4xL4", .node_index = 0},
                  nullptr, "2026-10-04T00:00:00Z");
    ASSERT_TRUE(valid("meta", doc));
    EXPECT_EQ(doc["driver_version"], "unknown");
    EXPECT_EQ(doc["cuda_version"], "unknown");
    EXPECT_EQ(doc["tool_version"], OSTIA_TOOL_VERSION);
    EXPECT_EQ(doc["provider"], "runpod");
    EXPECT_EQ(doc["instance_type"], "4xL4");
    EXPECT_EQ(doc["captured_at"], "2026-10-04T00:00:00Z");
    EXPECT_EQ(doc["schema"], 1);
}

TEST(MetaJson, VersionsFromNvml) {
    const NvmlFacts facts = three_gpus_out_of_order();
    const json doc = meta_json(
        MetaFlags{.provider = "aws", .instance_type = "g6.4xlarge", .node_index = std::nullopt},
        &facts, "t");
    ASSERT_TRUE(valid("meta", doc));
    EXPECT_EQ(doc["driver_version"], "550.54.15");
    EXPECT_EQ(doc["cuda_version"], "12.4");
}

TEST(Dump, IndentsAndEndsWithANewline) {
    EXPECT_EQ(dump(json{{"b", 1}, {"a", json::array()}}), "{\n  \"a\": [],\n  \"b\": 1\n}\n");
}
