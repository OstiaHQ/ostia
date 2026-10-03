#include "topology/model.hpp"

#include <algorithm>
#include <cassert>
#include <tuple>

namespace ostia::fabric::topology {

std::string_view to_string(NodeKind kind) {
    switch (kind) {
    case NodeKind::package:
        return "package";
    case NodeKind::numa:
        return "numa";
    case NodeKind::pcie_bridge:
        return "pcie_bridge";
    case NodeKind::gpu:
        return "gpu";
    case NodeKind::switch_group:
        return "switch_group";
    case NodeKind::nic:
        return "nic";
    }
    return "unknown";
}

std::string_view to_string(EdgeKind kind) {
    switch (kind) {
    case EdgeKind::pcie:
        return "pcie";
    case EdgeKind::nvlink:
        return "nvlink";
    case EdgeKind::numa_local:
        return "numa_local";
    }
    return "unknown";
}

namespace {

// A colliding attribute would silently overwrite a structural field.
template <typename Attrs> void check_attr_names(const Attrs& attrs) {
    for (const auto& [name, value] : attrs) {
        assert(name != "key" && name != "kind" && name != "from" && name != "to");
        (void)name;
    }
}

nlohmann::json node_json(const Node& node) {
    check_attr_names(node.attrs);
    nlohmann::json j = {{"key", node.key}, {"kind", to_string(node.kind)}};
    for (const auto& [name, value] : node.attrs) {
        // The analyzer loses track of the active alternative inside libstdc++'s std::visit.
        // NOLINTNEXTLINE(clang-analyzer-core.CallAndMessage)
        std::visit([&](const auto& v) { j[name] = v; }, value);
    }
    return j;
}

nlohmann::json edge_json(const Edge& edge) {
    check_attr_names(edge.attrs);
    nlohmann::json j = {{"kind", to_string(edge.kind)}, {"from", edge.from}, {"to", edge.to}};
    for (const auto& [name, value] : edge.attrs) {
        j[name] = value;
    }
    return j;
}

} // namespace

nlohmann::json to_json(const Model& model) {
    std::vector<const Node*> nodes;
    nodes.reserve(model.nodes.size());
    for (const Node& n : model.nodes)
        nodes.push_back(&n);
    std::ranges::stable_sort(nodes, [](const Node* a, const Node* b) { return a->key < b->key; });

    // Kinds order by their string names, not enum order: this fixes the golden edge order.
    std::vector<const Edge*> edges;
    edges.reserve(model.edges.size());
    for (const Edge& e : model.edges)
        edges.push_back(&e);
    std::ranges::stable_sort(edges, [](const Edge* a, const Edge* b) {
        return std::make_tuple(to_string(a->kind), std::string_view(a->from),
                               std::string_view(a->to)) < std::make_tuple(to_string(b->kind),
                                                                          std::string_view(b->from),
                                                                          std::string_view(b->to));
    });

    nlohmann::json out = {{"edges", nlohmann::json::array()}, {"nodes", nlohmann::json::array()}};
    for (const Edge* e : edges)
        out["edges"].push_back(edge_json(*e));
    for (const Node* n : nodes)
        out["nodes"].push_back(node_json(*n));
    return out;
}

std::string canonical_dump(const Model& model) { return to_json(model).dump(2) + "\n"; }

} // namespace ostia::fabric::topology
