#pragma once

#include <filesystem>
#include <functional>
#include <hwloc.h>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

#include "core/apis.hpp"
#include "core/diagnostics.hpp"
#include "core/sysfs.hpp"
#include "core/writers.hpp"
#include "topology/source.hpp"

namespace ostia::fabric::topology::capture {

// Fault injection for tests; both are empty in the tool.
struct PipelineHooks {
    std::function<void(std::string& xml)> before_reimport;
    // Runs on the staged data files after they are written and before they are validated.
    std::function<void(const std::filesystem::path& staged)> before_validate;
    // Runs in --out after diagnostics.txt is written and before the manifest; a throw stands
    // for a failed write there.
    std::function<void(const std::filesystem::path& out)> before_manifest;
    // Runs in print_id after the capture into its temporary directory returns and before that
    // directory is removed: the window a late signal can hit.
    std::function<void(const std::filesystem::path& dir)> after_print_id_capture;
};

struct Inputs {
    // Loaded by the caller, which times the load: the pipeline only reads it.
    hwloc_topology_t topo = nullptr;
    NvmlApi* nvml = nullptr; // nullptr: libnvidia-ml could not be loaded
    VerbsApi* verbs = nullptr;
    const SysfsReader* sysfs = nullptr;
    MetaFlags meta;
    std::optional<std::filesystem::path> links;
    std::vector<std::string> extra;
    // Adds the host's interfaces, hostname and machine-id to the leak check's raw set; off in
    // tests, which must not depend on the machine running them.
    bool live_host = false;
    PipelineHooks hooks;
};

struct CaptureOutcome {
    int exit_code;
    std::optional<std::string> topology_id; // set only for exit 0 and 2 (RFC-0003 §4)
};

// RFC-0003 §1-§4: stage, validate and leak-check the files in a private scratch directory under
// $TMPDIR, copy them into out only when both pass, replay them for the topology id, and write
// diagnostics.txt and the manifest last. A non-empty out without manifest.json is refused
// untouched (exit 1, no manifest); a previous capture in out is removed first. The stderr
// summary is printed here.
CaptureOutcome capture(const Inputs& in, const std::filesystem::path& out, Diagnostics& diag);

// The capture into a private temporary directory that is removed on every path; main prints
// only the id.
CaptureOutcome print_id(const Inputs& in, Diagnostics& diag);

// What the writers would see, as replay Facts, for comparing live and replayed models.
Facts live_facts(const Inputs& in);

// A failed manifest and diagnostics.txt for a failure before capture could run (hwloc_load, an
// exception in main), under capture's --out rules: out is created, a previous capture cleared,
// and a non-empty directory holding no capture refused untouched. Returns exit 1.
CaptureOutcome write_failed_capture(const std::filesystem::path& out, std::string_view code,
                                    Diagnostics& diag);

} // namespace ostia::fabric::topology::capture
