#pragma once

#include <cstdint>
#include <map>
#include <nlohmann/json.hpp>
#include <string>
#include <string_view>
#include <variant>
#include <vector>

#include "topology/error.hpp"

namespace ostia::fabric::topology {

enum class NodeKind { package, numa, pcie_bridge, gpu, switch_group, nic };
enum class EdgeKind { pcie, nvlink, numa_local };

// Integers only: a float would make the golden JSON depend on the printer (RFC-0003 §6).
using Value = std::variant<std::int64_t, std::string>;

struct Node {
    NodeKind kind;
    std::string key; // PCI bus ID, or a placeholder (RFC-0003 §6)
    std::map<std::string, Value> attrs;
};

struct Edge {
    EdgeKind kind;
    std::string from, to; // node keys
    std::map<std::string, std::int64_t> attrs;
};

struct Model {
    std::vector<Node> nodes; // any order; to_json sorts
    std::vector<Edge> edges;
};

std::string_view to_string(NodeKind kind);
std::string_view to_string(EdgeKind kind);

// Sorted and key-ordered, so equal models serialise to equal bytes.
nlohmann::json to_json(const Model& model);

// The golden-file form: to_json(model).dump(2) plus a trailing newline.
std::string canonical_dump(const Model& model);

} // namespace ostia::fabric::topology
