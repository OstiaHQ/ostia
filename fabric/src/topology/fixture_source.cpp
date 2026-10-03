#include "topology/fixture_source.hpp"

#include <algorithm>
#include <array>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <hwloc.h>
#include <memory>

#include "topology/error.hpp"
#include "topology/schema.hpp"

namespace ostia::fabric::topology {

namespace {

namespace fs = std::filesystem;
using nlohmann::json;

struct TopologyDeleter {
    void operator()(hwloc_topology* topo) const { hwloc_topology_destroy(topo); }
};
using TopologyPtr = std::unique_ptr<hwloc_topology, TopologyDeleter>;

std::string bus_id(unsigned domain, unsigned bus, unsigned dev, unsigned func) {
    std::array<char, 32> buf{};
    std::snprintf(buf.data(), buf.size(), "%04x:%02x:%02x.%01x", domain, bus, dev, func);
    return buf.data();
}

std::string host_bridge_key(unsigned domain, unsigned bus) {
    std::array<char, 40> buf{};
    std::snprintf(buf.data(), buf.size(), "hostbridge-%04x:%02x", domain, bus);
    return buf.data();
}

// A PCI bridge has an upstream bus ID; a host bridge does not.
bool has_bus_id(hwloc_obj_t obj) {
    return obj->type == HWLOC_OBJ_PCI_DEVICE ||
           (obj->type == HWLOC_OBJ_BRIDGE &&
            obj->attr->bridge.upstream_type == HWLOC_OBJ_BRIDGE_PCI);
}

std::string key_of(hwloc_obj_t obj) {
    if (obj->type == HWLOC_OBJ_PCI_DEVICE) {
        const auto& p = obj->attr->pcidev;
        return bus_id(p.domain, p.bus, p.dev, p.func);
    }
    if (has_bus_id(obj)) {
        const auto& p = obj->attr->bridge.upstream.pci;
        return bus_id(p.domain, p.bus, p.dev, p.func);
    }
    const auto& d = obj->attr->bridge.downstream.pci;
    return host_bridge_key(d.domain, d.secondary_bus);
}

int info_int(hwloc_obj_t obj, const char* name) {
    const char* value = hwloc_obj_get_info_by_name(obj, name);
    return value ? std::atoi(value) : 0;
}

PciFacts pci_facts(hwloc_topology_t topo, hwloc_obj_t obj) {
    PciFacts f;
    f.bridge = obj->type == HWLOC_OBJ_BRIDGE;
    f.key = key_of(obj);
    if (has_bus_id(obj)) {
        f.bus_id = f.key;
    }
    if (obj->type == HWLOC_OBJ_PCI_DEVICE) {
        f.pci_class = obj->attr->pcidev.class_id;
        f.pci_vendor = obj->attr->pcidev.vendor_id;
        f.pci_device = obj->attr->pcidev.device_id;
    } else if (has_bus_id(obj)) {
        f.pci_class = obj->attr->bridge.upstream.pci.class_id;
        f.pci_vendor = obj->attr->bridge.upstream.pci.vendor_id;
        f.pci_device = obj->attr->bridge.upstream.pci.device_id;
    }
    f.max_gen = info_int(obj, "OstiaPCIeMaxGen");
    f.max_width = info_int(obj, "OstiaPCIeMaxWidth");

    hwloc_obj_t parent = obj->parent;
    if (parent && (parent->type == HWLOC_OBJ_BRIDGE || parent->type == HWLOC_OBJ_PCI_DEVICE)) {
        f.parent_key = key_of(parent);
    }
    hwloc_obj_t anc = hwloc_get_non_io_ancestor_obj(topo, obj);
    if (anc && anc->nodeset) {
        for (int i = hwloc_bitmap_first(anc->nodeset); i != -1;
             i = hwloc_bitmap_next(anc->nodeset, i)) {
            f.numa.push_back(i);
        }
    }
    return f;
}

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

    const int packages = hwloc_get_nbobjs_by_type(topo.get(), HWLOC_OBJ_PACKAGE);
    facts_.packages = packages < 0 ? 0 : packages;
    const int pus = hwloc_get_nbobjs_by_type(topo.get(), HWLOC_OBJ_PU);
    facts_.pus = pus < 0 ? 0 : pus;
    for (hwloc_obj_t n = hwloc_get_next_obj_by_type(topo.get(), HWLOC_OBJ_NUMANODE, nullptr); n;
         n = hwloc_get_next_obj_by_type(topo.get(), HWLOC_OBJ_NUMANODE, n)) {
        facts_.numa_nodes.push_back(static_cast<int>(n->os_index));
    }
    std::ranges::sort(facts_.numa_nodes);

    for (hwloc_obj_t b = hwloc_get_next_bridge(topo.get(), nullptr); b;
         b = hwloc_get_next_bridge(topo.get(), b)) {
        facts_.pci.push_back(pci_facts(topo.get(), b));
    }
    for (hwloc_obj_t p = hwloc_get_next_pcidev(topo.get(), nullptr); p;
         p = hwloc_get_next_pcidev(topo.get(), p)) {
        facts_.pci.push_back(pci_facts(topo.get(), p));
    }

    facts_.nvml = read_json(dir, "nvml.json", false, "nvml");
    facts_.nics = read_json(dir, "nics.json", true, "nics");
    facts_.links = read_json(dir, "links.json", false, "links");
}

Facts FixtureSource::facts() const { return facts_; }

} // namespace ostia::fabric::topology
