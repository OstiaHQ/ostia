// topo1: an exact canonical form of the stripped topology graph (RFC-0003 §5).
//
//   labels ──► refine to a stable partition ──► all cells singletons? ──yes──► leaf certificate
//                     ▲                                │ no
//                     │                                ▼
//                     └──── individualise one vertex per twin class of the first open cell
//
// Refining to a fixed point subsumes the RFC's three Weisfeiler–Lehman rounds, and the search
// makes the form exact: a cycle of six and two triangles refine identically but give different
// certificates. The smallest certificate over the explored leaves is the canonical one. Twins
// (same colour, same labelled neighbourhood apart from each other) are exchanged by an
// automorphism that fixes the current partition, so exploring one per class keeps the minimum.

#include "topology/identity.hpp"

#include <algorithm>
#include <string>
#include <tuple>
#include <unordered_map>
#include <utility>
#include <vector>

#include "topology/error.hpp"
#include "topology/sha256.hpp"

namespace ostia::fabric::topology {

namespace {

// Identity attributes per kind (RFC-0003 §5); every other attribute is data and never reaches
// topo1.
std::vector<std::string> identity_attrs(NodeKind kind) {
    switch (kind) {
    case NodeKind::gpu:
        return {"model", "cc_major", "cc_minor"};
    case NodeKind::nic:
        return {"pci_vendor", "pci_device"};
    default:
        return {};
    }
}

std::vector<std::string> identity_attrs(EdgeKind kind) {
    switch (kind) {
    case EdgeKind::pcie:
        return {"gen", "width"};
    case EdgeKind::nvlink:
        return {"links"};
    default:
        return {};
    }
}

nlohmann::json node_label(const Node& node) {
    nlohmann::json j = {{"kind", to_string(node.kind)}};
    for (const std::string& name : identity_attrs(node.kind)) {
        auto it = node.attrs.find(name);
        if (it != node.attrs.end())
            std::visit([&](const auto& v) { j[name] = v; }, it->second);
    }
    return j;
}

nlohmann::json edge_label(const Edge& edge) {
    nlohmann::json j = {{"kind", to_string(edge.kind)}};
    for (const std::string& name : identity_attrs(edge.kind)) {
        auto it = edge.attrs.find(name);
        if (it != edge.attrs.end())
            j[name] = it->second;
    }
    return j;
}

// nvlink is undirected; pcie and numa_local point parent→child and device→NUMA node.
bool directed(EdgeKind kind) { return kind != EdgeKind::nvlink; }

enum Dir : int { kUndirected = 0, kOut = 1, kIn = 2 };

struct Arc {
    int label; // rank of the edge label among the graph's distinct labels
    int dir;
    int to;
};

struct StoredEdge {
    int from, to, label;
    bool directed;
};

// Keys are gone: a vertex is an index whose only meaning is its label and its arcs.
struct Graph {
    std::vector<std::string> vkey; // label as sorted-key JSON, the initial colour's source
    std::vector<nlohmann::json> vlabel;
    std::vector<nlohmann::json> elabel; // by rank
    std::vector<std::vector<Arc>> adj;
    std::vector<StoredEdge> edges;
};

// Ranks of the sorted distinct values, so the numbering depends on the values alone.
std::vector<int> rank_values(const std::vector<std::string>& values) {
    std::vector<std::string> distinct = values;
    std::sort(distinct.begin(), distinct.end());
    distinct.erase(std::unique(distinct.begin(), distinct.end()), distinct.end());
    std::vector<int> ranks;
    ranks.reserve(values.size());
    for (const std::string& v : values)
        ranks.push_back(
            int(std::lower_bound(distinct.begin(), distinct.end(), v) - distinct.begin()));
    return ranks;
}

Graph strip(const Model& model) {
    if (model.nodes.size() > kMaxNodes)
        throw TopologyError("node_cap", "",
                            std::to_string(model.nodes.size()) + " nodes exceed the cap of " +
                                std::to_string(kMaxNodes));
    Graph g;
    std::unordered_map<std::string, int> index;
    for (const Node& n : model.nodes) {
        if (!index.emplace(n.key, int(g.vlabel.size())).second)
            throw TopologyError("duplicate_key", "", "node key '" + n.key + "' appears twice");
        g.vlabel.push_back(node_label(n));
        g.vkey.push_back(g.vlabel.back().dump());
    }
    g.adj.resize(g.vlabel.size());

    std::vector<nlohmann::json> labels;
    std::vector<std::string> label_keys;
    for (const Edge& e : model.edges) {
        labels.push_back(edge_label(e));
        label_keys.push_back(labels.back().dump());
    }
    std::vector<int> ranks = rank_values(label_keys);
    g.elabel.resize(labels.size());
    for (std::size_t i = 0; i < labels.size(); ++i)
        g.elabel[std::size_t(ranks[i])] = labels[i];

    for (std::size_t i = 0; i < model.edges.size(); ++i) {
        const Edge& e = model.edges[i];
        auto from = index.find(e.from);
        auto to = index.find(e.to);
        if (from == index.end() || to == index.end())
            throw TopologyError("dangling_reference", "",
                                "edge " + e.from + " -> " + e.to + " names an unknown node");
        bool d = directed(e.kind);
        g.edges.push_back({from->second, to->second, ranks[i], d});
        g.adj[std::size_t(from->second)].push_back({ranks[i], d ? kOut : kUndirected, to->second});
        g.adj[std::size_t(to->second)].push_back({ranks[i], d ? kIn : kUndirected, from->second});
    }
    return g;
}

std::size_t count_distinct(std::vector<int> colour) {
    std::sort(colour.begin(), colour.end());
    return std::size_t(std::unique(colour.begin(), colour.end()) - colour.begin());
}

// Returns dense ranks 0..k-1 of the coarsest stable partition finer than `colour`. The old
// colour leads each signature, so the ranks keep the incoming order of the cells.
std::vector<int> refine(const Graph& g, std::vector<int> colour) {
    using Signature = std::pair<int, std::vector<std::tuple<int, int, int>>>;
    const std::size_t n = colour.size();
    std::size_t cells = count_distinct(colour);
    std::vector<Signature> sig(n);
    for (;;) {
        for (std::size_t v = 0; v < n; ++v) {
            sig[v].first = colour[v];
            sig[v].second.clear();
            for (const Arc& a : g.adj[v])
                sig[v].second.emplace_back(a.label, a.dir, colour[std::size_t(a.to)]);
            std::sort(sig[v].second.begin(), sig[v].second.end());
        }
        std::vector<Signature> distinct = sig;
        std::sort(distinct.begin(), distinct.end());
        distinct.erase(std::unique(distinct.begin(), distinct.end()), distinct.end());
        for (std::size_t v = 0; v < n; ++v)
            colour[v] =
                int(std::lower_bound(distinct.begin(), distinct.end(), sig[v]) - distinct.begin());
        if (distinct.size() == cells)
            return colour;
        cells = distinct.size();
    }
}

// Exchanging u and v is an automorphism fixing every other vertex: the same arcs to third
// vertices, mirror-equal arcs between the two (a directed u→v needs a v→u), and self-loops
// alike. The caller passes vertices of one colour, so their labels are equal too.
bool twins(const Graph& g, int u, int v) {
    constexpr int kSelf = -1;
    auto profile = [&](int x, int other) {
        std::vector<std::tuple<int, int, int>> rest;
        std::vector<std::pair<int, int>> mutual;
        for (const Arc& a : g.adj[std::size_t(x)]) {
            if (a.to == other)
                mutual.emplace_back(a.label, a.dir);
            else
                rest.emplace_back(a.label, a.dir, a.to == x ? kSelf : a.to);
        }
        std::sort(rest.begin(), rest.end());
        std::sort(mutual.begin(), mutual.end());
        return std::make_pair(std::move(rest), std::move(mutual));
    };
    return profile(u, v) == profile(v, u);
}

// The graph relabelled by `position`: equal certificates mean isomorphic stripped graphs.
std::string certificate(const Graph& g, const std::vector<int>& position) {
    std::vector<std::tuple<int, int, int>> edges;
    edges.reserve(g.edges.size());
    for (const StoredEdge& e : g.edges) {
        int a = position[std::size_t(e.from)];
        int b = position[std::size_t(e.to)];
        if (!e.directed && b < a)
            std::swap(a, b);
        edges.emplace_back(a, b, e.label);
    }
    std::sort(edges.begin(), edges.end());

    std::vector<std::size_t> order(g.vlabel.size());
    for (std::size_t v = 0; v < order.size(); ++v)
        order[std::size_t(position[v])] = v;

    nlohmann::json out = {{"edges", nlohmann::json::array()}, {"nodes", nlohmann::json::array()}};
    for (const auto& [a, b, label] : edges)
        out["edges"].push_back({a, b, g.elabel[std::size_t(label)]});
    for (std::size_t v : order)
        out["nodes"].push_back(g.vlabel[v]);
    return out.dump();
}

// The search defines topo1: branch on the first non-singleton cell, refine to a fixed point,
// keep the smallest certificate. Changing any of the three changes every id. Pruning may change
// only if it keeps that minimum; orbit pruning for symmetric shapes that are not twins is a
// planned follow-up (RFC-0003 Performance).
struct Search {
    const Graph& g;
    std::size_t leaves = 0;
    std::string best;
    bool found = false;

    void run(const std::vector<int>& colour) {
        const std::size_t n = colour.size();
        std::vector<std::vector<int>> cells(n);
        for (std::size_t v = 0; v < n; ++v)
            cells[std::size_t(colour[v])].push_back(int(v));
        auto open = std::find_if(cells.begin(), cells.end(),
                                 [](const std::vector<int>& c) { return c.size() > 1; });
        if (open == cells.end()) {
            if (++leaves > kMaxLeaves)
                throw TopologyError("leaf_cap", "",
                                    "canonical search exceeded " + std::to_string(kMaxLeaves) +
                                        " leaves");
            std::string cert = certificate(g, colour);
            if (!found || cert < best) {
                best = std::move(cert);
                found = true;
            }
            return;
        }
        // Which vertex represents a twin class depends on vertex order, but every skipped vertex
        // is a twin of an explored one, so the minimum over the explored leaves does not.
        std::vector<int> representatives;
        for (int v : *open) {
            bool covered = std::any_of(representatives.begin(), representatives.end(),
                                       [&](int r) { return twins(g, r, v); });
            if (!covered)
                representatives.push_back(v);
        }
        for (int v : representatives) {
            std::vector<int> next(n);
            for (std::size_t x = 0; x < n; ++x)
                next[x] = 2 * colour[x] + 1;
            next[std::size_t(v)] = 2 * colour[std::size_t(v)];
            run(refine(g, std::move(next)));
        }
    }
};

// Each rail with its node-0 and node-1 entries exchanged; other fields and rail order stay.
nlohmann::json swap_ends(nlohmann::json rails) {
    if (!rails.is_array())
        return rails;
    for (nlohmann::json& rail : rails) {
        if (!rail.is_object())
            continue;
        nlohmann::json first = rail.contains("node-0") ? rail["node-0"] : nlohmann::json();
        nlohmann::json second = rail.contains("node-1") ? rail["node-1"] : nlohmann::json();
        rail.erase("node-0");
        rail.erase("node-1");
        if (!second.is_null())
            rail["node-0"] = std::move(second);
        if (!first.is_null())
            rail["node-1"] = std::move(first);
    }
    return rails;
}

} // namespace

std::string canonical_identity_json(const Model& model) {
    Graph g = strip(model);
    Search search{g};
    search.run(refine(g, rank_values(g.vkey)));
    return search.best;
}

std::string topo1(const Model& model) {
    return "topo1:sha256:" + sha256_hex(canonical_identity_json(model));
}

std::string pair_id(const std::string& id0, const std::string& id1, const nlohmann::json& pair) {
    const std::string& lo = std::min(id0, id1);
    const std::string& hi = std::max(id0, id1);
    const nlohmann::json rails = pair.contains("rails") ? pair.at("rails") : nlohmann::json();
    auto structural = [&](nlohmann::json r) {
        nlohmann::json doc = {
            {"nodes", {lo, hi}},
            {"link_class", pair.contains("link_class") ? pair.at("link_class") : nlohmann::json()},
            {"rails", std::move(r)},
        };
        return doc.dump();
    };
    // Sorting the ids renames the endpoints, so each rail's node-0/node-1 must follow its id
    // (RFC-0003 §7). Rail order stays: pair.json's `measured` refers to rails by index.
    if (id0 != id1)
        return "topo1:sha256:" + sha256_hex(structural(id1 < id0 ? swap_ends(rails) : rails));
    // Equal ids leave no order to follow, so take the smaller of the two orientations.
    const std::string given = structural(rails);
    const std::string swapped = structural(swap_ends(rails));
    return "topo1:sha256:" + sha256_hex(std::min(given, swapped));
}

} // namespace ostia::fabric::topology
