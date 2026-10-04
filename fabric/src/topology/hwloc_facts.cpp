#include "topology/hwloc_facts.hpp"

#include <algorithm>
#include <array>
#include <cstdio>
#include <cstdlib>

namespace ostia::fabric::topology {

namespace {

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

PciFacts pci_facts(hwloc_topology_t topo, hwloc_obj_t obj, const PcieMaxMap* pcie_max) {
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
    if (pcie_max == nullptr) {
        f.max_gen = info_int(obj, "OstiaPCIeMaxGen");
        f.max_width = info_int(obj, "OstiaPCIeMaxWidth");
    } else if (const auto it = pcie_max->find(f.bus_id);
               !f.bus_id.empty() && it != pcie_max->end()) {
        f.max_gen = it->second.gen;
        f.max_width = it->second.width;
    }

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

} // namespace

void extract_hwloc_facts(hwloc_topology_t topo, const PcieMaxMap* pcie_max, Facts& facts) {
    const int packages = hwloc_get_nbobjs_by_type(topo, HWLOC_OBJ_PACKAGE);
    facts.packages = packages < 0 ? 0 : packages;
    const int pus = hwloc_get_nbobjs_by_type(topo, HWLOC_OBJ_PU);
    facts.pus = pus < 0 ? 0 : pus;
    for (hwloc_obj_t n = hwloc_get_next_obj_by_type(topo, HWLOC_OBJ_NUMANODE, nullptr); n;
         n = hwloc_get_next_obj_by_type(topo, HWLOC_OBJ_NUMANODE, n)) {
        facts.numa_nodes.push_back(static_cast<int>(n->os_index));
    }
    std::ranges::sort(facts.numa_nodes);

    for (hwloc_obj_t b = hwloc_get_next_bridge(topo, nullptr); b;
         b = hwloc_get_next_bridge(topo, b)) {
        facts.pci.push_back(pci_facts(topo, b, pcie_max));
    }
    for (hwloc_obj_t p = hwloc_get_next_pcidev(topo, nullptr); p;
         p = hwloc_get_next_pcidev(topo, p)) {
        facts.pci.push_back(pci_facts(topo, p, pcie_max));
    }
}

} // namespace ostia::fabric::topology
