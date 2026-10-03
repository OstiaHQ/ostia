#include <gtest/gtest.h>
#include <nlohmann/json.hpp>
#include <set>

#include "topology/error.hpp"
#include "topology/schema.hpp"

using namespace ostia::fabric::topology;
using nlohmann::json;

namespace {

json minimal_nvml() {
    return json::parse(R"({"schema":1,"gpus":[{"bus_id":"0000:07:00.0","name":"NVIDIA L4",
    "cc_major":8,"cc_minor":9,"memory_bytes":24152899584,"cuda_ordinal":0,
    "nvlinks":"not_supported"}],"p2p":[]})");
}

// Keywords whose value is a schema or a list of schemas; any other keyword is a leaf.
void audit_keywords(const json& schema, std::set<std::string>& used) {
    for (const auto& [key, value] : schema.items()) {
        used.insert(key);
        if (key == "properties") {
            for (const auto& [name, sub] : value.items())
                audit_keywords(sub, used);
        } else if (key == "items") {
            audit_keywords(value, used);
        } else if (key == "oneOf") {
            for (const auto& sub : value)
                audit_keywords(sub, used);
        }
    }
}

} // namespace

TEST(Schema, MinimalDocumentsPass) {
    EXPECT_TRUE(validate("nvml", minimal_nvml()).empty());
    EXPECT_TRUE(
        validate("nics",
                 json::parse(R"({"schema":1,"rdma_probe":"unavailable","nics":[],"rdma":[]})"))
            .empty());
}

// RFC-0003 Testing: "Schema validation rejects an unexpected field in each file type".
TEST(Schema, UnexpectedFieldIsRejectedInEveryFileType) {
    auto nvml = minimal_nvml();
    nvml["gpus"][0]["uuid"] = "GPU-1234";
    auto errors = validate("nvml", nvml);
    ASSERT_EQ(errors.size(), 1u);
    EXPECT_EQ(errors[0].path, "/gpus/0/uuid");
    for (auto [name, doc] : std::initializer_list<std::pair<const char*, const char*>>{
             {"nics", R"({"schema":1,"rdma_probe":"ok","nics":[],"rdma":[],"mac":"x"})"},
             {"links", R"({"schema":1,"links":[],"host":"x"})"},
             {"meta", R"({"schema":1,"provider":"p","instance_type":"i","driver_version":"580.1",
                       "cuda_version":"13.0","tool_version":"0.1.0","captured_at":"2026-10-03T00:00:00Z",
                       "hostname":"x"})"},
             {"manifest",
              R"({"schema":1,"tool_version":"0.1.0","status":"complete","sanitized":true,
                           "leak_check":"passed","topology_id":"topo1:sha256:00","files":{},
                           "missing":[],"errors":[],"extra":1})"},
             {"pair", R"({"schema":1,"nodes":["node-0","node-1"],"link_class":"tcp","rails":[],
                       "measured":[],"extra":1})"}}) {
        EXPECT_FALSE(validate(name, json::parse(doc)).empty()) << name;
    }
}

TEST(Schema, WrongVersionAndBadEnumAreRejected) {
    auto doc = minimal_nvml();
    doc["schema"] = 2;
    EXPECT_FALSE(validate("nvml", doc).empty());
    doc = minimal_nvml();
    doc["gpus"][0]["nvlinks"] = "maybe"; // neither an array nor "not_supported" (R8 oneOf)
    EXPECT_FALSE(validate("nvml", doc).empty());
}

TEST(Schema, NicPortFactsMayBeUnknown) { // D4, R8
    auto nics = json::parse(R"({"schema":1,"rdma_probe":"unavailable","rdma":[],"nics":[
    {"bus_id":"0000:3b:00.0","driver":"mlx5_core","link_layer":"unknown",
     "max_pcie_gen":4,"max_pcie_width":16,"numa_node":0,"port_speed_mbps":"unknown"}]})");
    EXPECT_TRUE(validate("nics", nics).empty());
    nics["nics"][0]["port_speed_mbps"] = "fast";
    EXPECT_FALSE(validate("nics", nics).empty());
    nics["nics"][0].erase("port_speed_mbps"); // always present (R8)
    EXPECT_FALSE(validate("nics", nics).empty());
}

TEST(Schema, UnknownSchemaNameThrows) {
    EXPECT_THROW(validate("hosts", json::object()), TopologyError);
}

TEST(Schema, EverySchemaUsesOnlySupportedKeywords) {
    const std::set<std::string> allowed = {
        "type",    "properties", "required",   "additionalProperties",
        "enum",    "const",      "items",      "oneOf",
        "pattern", "minimum",    "maximum",    "$schema",
        "$id",     "title",      "description"};
    ASSERT_EQ(embedded_schemas().size(), 6u);
    for (const auto& [name, schema] : embedded_schemas()) {
        std::set<std::string> used;
        audit_keywords(schema, used);
        for (const auto& keyword : used) {
            EXPECT_TRUE(allowed.contains(keyword)) << name << " uses " << keyword;
        }
    }
}
