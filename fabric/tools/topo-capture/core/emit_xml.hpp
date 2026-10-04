#pragma once

#include <hwloc.h>
#include <string>

#include "core/diagnostics.hpp"
#include "core/sysfs.hpp"
#include "topology/hwloc_facts.hpp"

namespace ostia::fabric::topology::capture {

// The topology rewritten to the RFC-0003 §2.1 allowlist: an explicit set of object types (every
// OS device and Misc object is dropped), their structural attributes, the allowlisted info keys,
// and the maximum PCIe link facts from pcie_max. Equal topologies give equal bytes.
std::string emit_xml(hwloc_topology_t topo, const PcieMaxMap& pcie_max, Diagnostics& diag);

// RFC-0003 §2.1: the emitted XML must load with the replay flags. Throws
// TopologyError{"xml", "hwloc.xml", ...} when it does not.
void reimport_check(const std::string& xml);

// sysfs max_link_speed/max_link_width per PCI bus ID; a device with neither has no entry, so
// the emitter omits its keys rather than writing zeros.
PcieMaxMap read_pcie_max(const SysfsReader& sysfs);

} // namespace ostia::fabric::topology::capture
