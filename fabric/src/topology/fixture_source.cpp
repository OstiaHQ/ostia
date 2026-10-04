#include "topology/fixture_source.hpp"

#include <fstream>
#include <hwloc.h>
#include <memory>

#include "topology/error.hpp"
#include "topology/hwloc_facts.hpp"
#include "topology/schema.hpp"

namespace ostia::fabric::topology {

namespace {

namespace fs = std::filesystem;
using nlohmann::json;

struct TopologyDeleter {
    void operator()(hwloc_topology* topo) const { hwloc_topology_destroy(topo); }
};
using TopologyPtr = std::unique_ptr<hwloc_topology, TopologyDeleter>;

json read_json(const fs::path& dir, const std::string& name, bool required, const char* schema) {
    const fs::path path = dir / name;
    if (!fs::exists(path)) {
        if (required) {
            throw TopologyError("missing_file", name, "not found in " + dir.string());
        }
        return nullptr;
    }
    // A directory opens as a stream on Linux and fails only on read, so check the type too.
    std::ifstream in(path);
    if (!in || !fs::is_regular_file(path)) {
        throw TopologyError("unreadable", name, "cannot be read in " + dir.string());
    }
    json doc;
    try {
        doc = json::parse(in);
    } catch (const json::parse_error& e) {
        throw TopologyError("schema", name, std::string("invalid JSON: ") + e.what());
    }
    const auto errors = validate(schema, doc);
    if (!errors.empty()) {
        std::string detail;
        for (const auto& e : errors) {
            detail += (detail.empty() ? "" : "; ") + e.path + " " + e.message;
        }
        if (doc.is_object() && doc.contains("schema") && doc["schema"] != kSchemaVersion) {
            detail += " (supported: " + std::to_string(kSchemaVersion) + ")";
        }
        throw TopologyError("schema", name, detail);
    }
    return doc;
}

} // namespace

FixtureSource::FixtureSource(const fs::path& dir) {
    const fs::path xml = dir / "hwloc.xml";
    if (!fs::exists(xml)) {
        throw TopologyError("missing_file", "hwloc.xml", "not found in " + dir.string());
    }

    hwloc_topology_t raw = nullptr;
    if (hwloc_topology_init(&raw) != 0) {
        throw TopologyError("xml", "hwloc.xml", "hwloc_topology_init failed");
    }
    TopologyPtr topo(raw);
    // RFC-0003 §2.1: keep PUs the capture host's cgroup disallowed, so replay is host-independent.
    hwloc_topology_set_flags(topo.get(), HWLOC_TOPOLOGY_FLAG_INCLUDE_DISALLOWED);
    hwloc_topology_set_io_types_filter(topo.get(), HWLOC_TYPE_FILTER_KEEP_IMPORTANT);
    if (hwloc_topology_set_xml(topo.get(), xml.c_str()) != 0 ||
        hwloc_topology_load(topo.get()) != 0) {
        throw TopologyError("xml", "hwloc.xml", "hwloc could not load the topology");
    }

    extract_hwloc_facts(topo.get(), nullptr, facts_);

    facts_.nvml = read_json(dir, "nvml.json", false, "nvml");
    facts_.nics = read_json(dir, "nics.json", true, "nics");
    facts_.links = read_json(dir, "links.json", false, "links");
}

Facts FixtureSource::facts() const { return facts_; }

} // namespace ostia::fabric::topology
