#pragma once

#include <nlohmann/json.hpp>
#include <string>
#include <vector>

namespace ostia::fabric::topology {

// One hwloc PCIDev or Bridge object.
struct PciFacts {
    std::string bus_id;     // "" for host bridges
    std::string key;        // bus_id, or "hostbridge-<domain>:<secondary bus>"
    std::string parent_key; // PCI parent's key; "" when the parent is a non-IO object
    bool bridge = false;
    int pci_class = 0, pci_vendor = 0, pci_device = 0;
    int max_gen = 0, max_width = 0; // OstiaPCIeMaxGen/Width info keys (R4); 0 = absent
    std::vector<int> numa;          // os_index of NUMA nodes local to the nearest non-IO ancestor
};

struct Facts {
    int packages = 0;
    std::vector<int> numa_nodes; // os_index, sorted
    std::vector<PciFacts> pci;   // hwloc order
    nlohmann::json nvml;         // validated nvml.json, or null when absent
    nlohmann::json nics;         // validated nics.json
    nlohmann::json links;        // validated links.json, or null
    int pus = 0; // PUs hwloc kept, disallowed ones included (D6); lets tests observe the flag
};

class TopologySource {
  public:
    virtual ~TopologySource() = default;
    virtual Facts facts() const = 0;
};

} // namespace ostia::fabric::topology
