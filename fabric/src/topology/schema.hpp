#pragma once

#include <map>
#include <nlohmann/json.hpp>
#include <string>
#include <string_view>
#include <vector>

namespace ostia::fabric::topology {

inline constexpr int kSchemaVersion = 1;

struct SchemaError {
    std::string path; // JSON Pointer into the document, e.g. "/gpus/0/name"
    std::string message;
};

// Validates against the embedded schema `name` (nvml, nics, links, meta, manifest or pair).
// Throws TopologyError("schema") for an unknown name. Reports every violation.
std::vector<SchemaError> validate(std::string_view name, const nlohmann::json& doc);

// The embedded schema documents, for tests that audit which keywords they use.
const std::map<std::string_view, nlohmann::json>& embedded_schemas();

} // namespace ostia::fabric::topology
