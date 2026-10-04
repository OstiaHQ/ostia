#pragma once

#include <hwloc.h>
#include <memory>

#include "core/apis.hpp"
#include "core/diagnostics.hpp"
#include "core/sysfs.hpp"

// The Linux-only sources (RFC-0003 §1): hwloc's own discovery, and NVML and ibverbs through
// dlopen, so the tool runs on machines without either library and never links them.
namespace ostia::fabric::topology::capture {

struct TopologyDeleter {
    void operator()(hwloc_topology* topo) const { hwloc_topology_destroy(topo); }
};
using TopologyPtr = std::unique_ptr<hwloc_topology, TopologyDeleter>;

// Discovery with the replay flags (RFC-0003 §2.1). hwloc itself honours HWLOC_XMLFILE, which is
// how tests substitute a machine. nullptr when hwloc cannot load a topology.
TopologyPtr load_live_topology(Diagnostics& diag);

// libnvidia-ml.so.1, or nullptr when it cannot be loaded. diag must outlive the result.
std::unique_ptr<NvmlApi> load_nvml(Diagnostics& diag);

// libibverbs.so.1, loaded on the first ports() call; bus IDs and the GPUDirect state come from
// the rooted sysfs. sysfs and diag must outlive the result.
std::unique_ptr<VerbsApi> make_verbs(const SysfsReader& sysfs, Diagnostics& diag);

} // namespace ostia::fabric::topology::capture
