#pragma once

#include <hwloc.h>
#include <map>
#include <memory>
#include <optional>
#include <string>

#include "topology/source.hpp"

namespace ostia::fabric::topology {

// The one owner type for a loaded hwloc topology: replay, the capture's re-import check and
// live discovery all release it the same way.
struct TopologyDeleter {
    void operator()(hwloc_topology* topo) const { hwloc_topology_destroy(topo); }
};
using TopologyPtr = std::unique_ptr<hwloc_topology, TopologyDeleter>;

// Maximum PCIe link generation and width; 0 means unknown (RFC-0003 §2.1).
struct PcieMax {
    int gen = 0, width = 0;
};
using PcieMaxMap = std::map<std::string, PcieMax>; // keyed by PCI bus ID

// "dddd:bb:dd.f" for a PCI device or a bridge with a PCI upstream; nullopt for host bridges and
// non-PCI objects. The one formatter for both the replay facts and the capture's PcieMaxMap keys,
// so the two cannot drift apart.
std::optional<std::string> bus_id_of(hwloc_obj_t obj);

// Fills packages, NUMA nodes, PUs and PCI facts from a loaded topology. With a map, the map
// supplies the maximum link facts (live capture, read from sysfs); without one, the
// OstiaPCIeMaxGen/OstiaPCIeMaxWidth info keys do (replay).
void extract_hwloc_facts(hwloc_topology_t topo, const PcieMaxMap* pcie_max, Facts& facts);

} // namespace ostia::fabric::topology
