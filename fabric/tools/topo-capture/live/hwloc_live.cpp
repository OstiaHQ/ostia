#include <cstdlib>
#include <filesystem>
#include <system_error>
#include <unistd.h>

#include "live/live.hpp"

namespace ostia::fabric::topology::capture {

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
        if (!std::filesystem::is_regular_file(xml, ec) || ::access(xml, R_OK) != 0) {
            diag.add("hwloc: HWLOC_XMLFILE names no readable file");
            return nullptr;
        }
        diag.add("hwloc: loading the XML file HWLOC_XMLFILE names, not this machine");
    }
    if (hwloc_topology_load(raw) != 0) {
        diag.add("hwloc: topology load failed");
        return nullptr;
    }
    // A cgroup-narrowed cpuset or nodeset is reported once, by emit_xml, which writes the file.
    return topo;
}

} // namespace ostia::fabric::topology::capture
