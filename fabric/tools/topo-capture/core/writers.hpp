#pragma once

#include <nlohmann/json.hpp>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

#include "core/apis.hpp"
#include "core/nics.hpp"
#include "core/result.hpp"

namespace ostia::fabric::topology::capture {

// node_index is recorded in diagnostics only: meta.json's schema has no field for it.
struct MetaFlags {
    std::string provider, instance_type;
    std::optional<int> node_index;
};

// NVML's "00000000:3B:00.0" to the schema's "0000:3b:00.0". A domain above 0xffff cannot be
// written in four digits and is refused ("bus_id_domain"); anything else malformed is
// "bus_id_format".
Result<std::string> normalize_bus_id(std::string_view text);

// RFC-0003 §2.2. GPUs are written in bus ID order and cuda_ordinal is that rank, which is what
// CUDA_DEVICE_ORDER=PCI_BUS_ID gives without loading the CUDA runtime. UUIDs, serials and board
// IDs are never written.
nlohmann::json nvml_json(const NvmlFacts& facts);

// RFC-0003 §2.3. nullopt ports means the verbs probe could not run ("unavailable"); an empty
// vector means it ran and found no devices ("ok").
nlohmann::json nics_json(const std::vector<NicFacts>& nics,
                         const std::optional<std::vector<VerbsPort>>& ports);

// RFC-0003 §2.4. Without NVML the driver and CUDA versions are "unknown".
nlohmann::json meta_json(const MetaFlags& flags, const NvmlFacts* nvml,
                         std::string_view captured_at);

// The on-disk form: two-space indent, sorted keys, trailing newline (as generate.py writes).
std::string dump(const nlohmann::json& doc);

} // namespace ostia::fabric::topology::capture
