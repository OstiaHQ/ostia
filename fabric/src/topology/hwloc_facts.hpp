#pragma once

#include <hwloc.h>
#include <map>
#include <string>

#include "topology/source.hpp"

namespace ostia::fabric::topology {

// Maximum PCIe link generation and width; 0 means unknown (RFC-0003 §2.1).
struct PcieMax {
    int gen = 0, width = 0;
};
using PcieMaxMap = std::map<std::string, PcieMax>; // keyed by PCI bus ID

// Fills packages, NUMA nodes, PUs and PCI facts from a loaded topology. With a map, the map
// supplies the maximum link facts (live capture, read from sysfs); without one, the
// OstiaPCIeMaxGen/OstiaPCIeMaxWidth info keys do (replay).
void extract_hwloc_facts(hwloc_topology_t topo, const PcieMaxMap* pcie_max, Facts& facts);

} // namespace ostia::fabric::topology
