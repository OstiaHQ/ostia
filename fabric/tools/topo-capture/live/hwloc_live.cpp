#include <cstdlib>
#include <filesystem>
#include <string>
#include <string_view>
#include <system_error>

#include "live/live.hpp"

namespace ostia::fabric::topology::capture {

namespace {

// A cgroup or cpuset can hide PUs and NUMA nodes from the capturing process; the capture keeps
// them (INCLUDE_DISALLOWED) but says so, as counts only.
void report_disallowed(hwloc_const_bitmap_t allowed, hwloc_const_bitmap_t complete,
                       std::string_view what, Diagnostics& diag) {
    if (allowed == nullptr || complete == nullptr || hwloc_bitmap_isequal(allowed, complete) != 0) {
        return;
    }
    std::string line = "hwloc: the allowed ";
    line += what;
    line += " set is narrower than the complete one (";
    line += std::to_string(hwloc_bitmap_weight(allowed));
    line += " of ";
    line += std::to_string(hwloc_bitmap_weight(complete));
    line += " allowed)";
    diag.add(line);
}

} // namespace

TopologyPtr load_live_topology(Diagnostics& diag) {
    hwloc_topology_t raw = nullptr;
    if (hwloc_topology_init(&raw) != 0) {
        diag.add("hwloc: topology init failed");
        return nullptr;
    }
    TopologyPtr topo(raw);
    // Without these the live tree and its replay would differ in disallowed PUs and I/O objects.
    if (hwloc_topology_set_flags(raw, HWLOC_TOPOLOGY_FLAG_INCLUDE_DISALLOWED) != 0 ||
        hwloc_topology_set_io_types_filter(raw, HWLOC_TYPE_FILTER_KEEP_IMPORTANT) != 0) {
        diag.add("hwloc: the replay flags were refused");
        return nullptr;
    }
    // hwloc_topology_load reads HWLOC_XMLFILE only when no backend was set, which none is here.
    // An unreadable file makes hwloc fall back to this machine, so a test would capture its
    // runner; refusing it keeps the substitution all or nothing.
    if (const char* xml = std::getenv("HWLOC_XMLFILE"); xml != nullptr) {
        std::error_code ec;
        if (!std::filesystem::is_regular_file(xml, ec)) {
            diag.add("hwloc: HWLOC_XMLFILE names no readable file");
            return nullptr;
        }
        diag.add("hwloc: loading the XML file HWLOC_XMLFILE names, not this machine");
    }
    if (hwloc_topology_load(raw) != 0) {
        diag.add("hwloc: topology load failed");
        return nullptr;
    }
    report_disallowed(hwloc_topology_get_allowed_cpuset(raw),
                      hwloc_topology_get_complete_cpuset(raw), "PU", diag);
    report_disallowed(hwloc_topology_get_allowed_nodeset(raw),
                      hwloc_topology_get_complete_nodeset(raw), "NUMA node", diag);
    return topo;
}

} // namespace ostia::fabric::topology::capture
