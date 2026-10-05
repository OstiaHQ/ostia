#pragma once

#include <string>
#include <string_view>
#include <variant>
#include <vector>

#include "core/apis.hpp"
#include "core/diagnostics.hpp"
#include "core/sysfs.hpp"

namespace ostia::fabric::topology::capture {

struct NicFacts {
    std::string bus_id, driver, link_layer;
    int max_gen, max_width, numa;
    std::variant<int, std::string> port_speed_mbps;
};

// PCIe generation for a sysfs max_link_speed such as "16.0 GT/s PCIe": 2.5 -> 1 through 64.0 -> 6.
// Anything else, including "Unknown", is 0.
int parse_pcie_gen(std::string_view text);

// NICs are found by PCI class (0x0200 ethernet, 0x0207 InfiniBand), not by interface name, so a
// NIC whose netdev lives in another network namespace is still listed. SR-IOV virtual functions
// are skipped and counted: a pod sees the host's whole PCI tree and VFs would inflate the model.
std::vector<NicFacts> scan_nics(const SysfsReader& sysfs, const std::vector<VerbsPort>& verbs,
                                Diagnostics& diag);

} // namespace ostia::fabric::topology::capture
