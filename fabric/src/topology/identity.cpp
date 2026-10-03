// topo1: an exact canonical form of the stripped topology graph (RFC-0003 §5).
//
//   labels ──► refine to a stable partition ──► all cells singletons? ──yes──► leaf certificate
//                     ▲                                │ no
//                     │                                ▼
//                     └──── individualise one vertex per orbit of the first open cell
//
// Refining to a fixed point subsumes the RFC's three Weisfeiler–Lehman rounds, and the search
// makes the form exact: a cycle of six and two triangles refine identically but give different
// certificates. The smallest certificate over the leaves is the canonical one, so the tree, the
// refinement and the certificate alone define the id. Pruning skips only subtrees that an
// automorphism maps onto explored ones, whose leaves carry the same certificates: twins, and
// the automorphisms that equal leaves reveal (RFC-0003 §5 and Performance).
#include "topology/identity.hpp"

#include <algorithm>
#include <cassert>
#include <cstddef>
#include <limits>
#include <numeric>
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

using Perm = std::vector<int>; // point → image

// Left to right: (a * b)[x] = b[a[x]].
Perm compose(const Perm& a, const Perm& b) {
    Perm out(a.size());
    for (std::size_t x = 0; x < a.size(); ++x)
        out[x] = b[std::size_t(a[x])];
    return out;
}

Perm inverse(const Perm& p) {
    Perm out(p.size());
    for (std::size_t x = 0; x < p.size(); ++x)
        out[std::size_t(p[x])] = int(x);
    return out;
}

bool is_identity(const Perm& p) {
    for (std::size_t x = 0; x < p.size(); ++x)
        if (p[x] != int(x))
            return false;
    return true;
}

int first_moved(const Perm& p) {
    for (std::size_t x = 0; x < p.size(); ++x)
        if (p[x] != int(x))
            return int(x);
    return -1;
}

// A base and strong generating set, built by deterministic Schreier–Sims (Seress, "Permutation
// Group Algorithms", ch. 4). Invariant once built: the strong generators that fix base[0..i-1]
// generate the pointwise stabiliser of those points, for every i.
class StabiliserChain {
  public:
    StabiliserChain(std::size_t n, std::vector<int> base, const std::vector<Perm>& gens)
        : n_(n), base_(std::move(base)), strong_(gens) {
        // A generator that fixes the whole base would belong to no level's check.
        for (const Perm& s : strong_)
            if (fixes_prefix(s, base_.size()))
                base_.push_back(first_moved(s));
        levels_.resize(base_.size());
        for (std::size_t i = base_.size(); i > 0;) {
            const std::size_t grown = close_level(i - 1);
            i = grown == kClosed ? i - 1 : grown + 1;
        }
    }

    // The strong generators that fix base[0..k-1]; they generate that pointwise stabiliser.
    std::vector<const Perm*> stabiliser_generators(std::size_t k) const {
        std::vector<const Perm*> out;
        for (const Perm& s : strong_)
            if (fixes_prefix(s, k))
                out.push_back(&s);
        return out;
    }

  private:
    static constexpr std::size_t kClosed = std::numeric_limits<std::size_t>::max();

    struct Level {
        std::vector<int> index; // point → its coset representative in reps, -1 off the orbit
        std::vector<Perm> reps; // reps[k] maps the level's base point to the k-th orbit point
        std::vector<Perm> inv;  // inv[k] is reps[k]'s inverse
    };

    bool fixes_prefix(const Perm& s, std::size_t k) const {
        for (std::size_t l = 0; l < k; ++l)
            if (s[std::size_t(base_[l])] != base_[l])
                return false;
        return true;
    }

    void build_level(std::size_t i) {
        Level& level = levels_[i];
        level.index.assign(n_, -1);
        level.reps.clear();
        level.inv.clear();
        Perm id(n_);
        std::iota(id.begin(), id.end(), 0);
        level.index[std::size_t(base_[i])] = 0;
        level.reps.push_back(std::move(id));
        std::vector<const Perm*> gens = stabiliser_generators(i);
        for (std::size_t k = 0; k < level.reps.size(); ++k) {
            const int y = level.reps[k][std::size_t(base_[i])];
            for (const Perm* s : gens) {
                const int z = (*s)[std::size_t(y)];
                if (level.index[std::size_t(z)] >= 0)
                    continue;
                level.index[std::size_t(z)] = int(level.reps.size());
                level.reps.push_back(compose(level.reps[k], *s));
            }
        }
        for (const Perm& u : level.reps)
            level.inv.push_back(inverse(u));
    }

    // Strips h in place through levels from..end; returns the level it stopped at (base size
    // when it passed every level).
    std::size_t sift(Perm& h, std::size_t from) const {
        for (std::size_t l = from; l < base_.size(); ++l) {
            const int k = levels_[l].index[std::size_t(h[std::size_t(base_[l])])];
            if (k < 0)
                return l;
            const Perm& w = levels_[l].inv[std::size_t(k)];
            for (int& x : h)
                x = w[std::size_t(x)];
        }
        return base_.size();
    }

    // Levels above i are complete. Checks every Schreier generator of level i; on the first that
    // does not sift to the identity, adds its residue as a strong generator and returns the level
    // where the sift stopped, which must then be closed again. kClosed when level i is complete.
    std::size_t close_level(std::size_t i) {
        build_level(i);
        const std::vector<const Perm*> gens = stabiliser_generators(i);
        const Level& level = levels_[i];
        Perm h(n_);
        for (std::size_t k = 0; k < level.reps.size(); ++k) {
            const Perm& u = level.reps[k];
            const int delta = u[std::size_t(base_[i])];
            for (const Perm* s : gens) {
                const Perm& v_inv =
                    level.inv[std::size_t(level.index[std::size_t((*s)[std::size_t(delta)])])];
                for (std::size_t x = 0; x < n_; ++x)
                    h[x] = v_inv[std::size_t((*s)[std::size_t(u[x])])];
                const std::size_t j = sift(h, i + 1);
                if (j == base_.size() && is_identity(h))
                    continue;
                // Growing levels_ and strong_ invalidates level, u and gens: return at once.
                if (j == base_.size()) {
                    base_.push_back(first_moved(h));
                    levels_.emplace_back();
                }
                // The residue fixes base[0..j-1] and moves base[j], so levels below j keep
                // their generators and stay complete.
                strong_.push_back(std::move(h));
                return j;
            }
        }
        return kClosed;
    }

    std::size_t n_;
    std::vector<int> base_;
    std::vector<Perm> strong_;
    std::vector<Level> levels_;
};

// The automorphisms found so far, as generators of the group they span.
class PermGroup {
  public:
    explicit PermGroup(std::size_t n) : n_(n) {}

    bool empty() const { return gens_.empty(); }
    std::size_t generators() const { return gens_.size(); }

    void add(const Perm& p) {
        if (!is_identity(p) && std::find(gens_.begin(), gens_.end(), p) == gens_.end())
            gens_.push_back(p);
    }

    // The orbits of `cell` under the pointwise stabiliser of `prefix`, each in cell order and
    // ordered by first member. The stabiliser must map `cell` to itself.
    std::vector<std::vector<int>> orbits_fixing(const std::vector<int>& prefix,
                                                const std::vector<int>& cell) const {
        std::vector<int> root(n_);
        std::iota(root.begin(), root.end(), 0);
        auto find = [&](int x) {
            while (root[std::size_t(x)] != x)
                x = root[std::size_t(x)] = root[std::size_t(root[std::size_t(x)])];
            return x;
        };
        if (!gens_.empty()) {
            StabiliserChain chain(n_, prefix, gens_);
            for (const Perm* s : chain.stabiliser_generators(prefix.size()))
                for (int x : cell) {
                    int a = find(x), b = find((*s)[std::size_t(x)]);
                    if (a != b)
                        root[std::size_t(std::max(a, b))] = std::min(a, b);
                }
        }
        std::vector<std::vector<int>> orbits;
        std::vector<int> slot(n_, -1);
        for (int x : cell) {
            int r = find(x);
            if (slot[std::size_t(r)] < 0) {
                slot[std::size_t(r)] = int(orbits.size());
                orbits.emplace_back();
            }
            orbits[std::size_t(slot[std::size_t(r)])].push_back(x);
        }
        return orbits;
    }

  private:
    std::size_t n_;
    std::vector<Perm> gens_;
};

[[maybe_unused]] bool is_automorphism(const Graph& g, const Perm& p) {
    for (std::size_t v = 0; v < p.size(); ++v)
        if (g.vkey[std::size_t(p[v])] != g.vkey[v])
            return false;
    auto edges = [&](const Perm& map) {
        std::vector<std::tuple<int, int, int>> out;
        for (const StoredEdge& e : g.edges) {
            int a = map[std::size_t(e.from)], b = map[std::size_t(e.to)];
            if (!e.directed && b < a)
                std::swap(a, b);
            out.emplace_back(a, b, e.label);
        }
        std::sort(out.begin(), out.end());
        return out;
    };
    Perm id(p.size());
    std::iota(id.begin(), id.end(), 0);
    return edges(p) == edges(id);
}

// The tree, the refinement and the certificate define topo1: branch on the first non-singleton
// cell, give the chosen vertex colour 2c and the rest of its cell 2c+1, refine to a fixed point,
// keep the smallest certificate. Changing any of them changes every id. Pruning keeps that
// minimum (RFC-0003 §5, Performance), by McKay's argument ("Practical graph isomorphism",
// 1981): refinement and the target cell commute with automorphisms, so an automorphism fixing
// a node's path maps child subtrees onto child subtrees with the same certificates.
//   - Equal certificates at two leaves give an automorphism γ between their orderings. A leaf's
//     ordering determines its path: every cell before the target cell is a singleton, so the
//     vertex individualised at each depth holds the first place of its cell's block. γ therefore
//     maps one path onto the other position by position.
//   - Backjump: γ fixes the paths' common prefix ν and maps the current child of ν onto the
//     stored leaf's, explored earlier, so the rest of the current child is redundant.
//   - Orbits: a child in the same orbit as an explored child, under the stabiliser of the path
//     in the group of found automorphisms, is redundant. Twins are exchanged by a transposition
//     that fixes the path, so a twin of an earlier child is redundant too.
// Redundant children are never entered, and their leaves do not count toward kMaxLeaves.
struct Search {
    static constexpr std::size_t kNoJump = std::numeric_limits<std::size_t>::max();

    struct Leaf {
        std::string cert;
        std::vector<int> position; // vertex → position, the leaf's final colour
        std::vector<int> path;     // the individualised vertices, root first
    };

    const Graph& g;
    PermGroup group{g.vlabel.size()};
    std::vector<int> path;
    std::size_t leaves = 0;
    Leaf first, best;
    bool found = false;

    explicit Search(const Graph& graph) : g(graph) {}

    // Returns the depth to resume at, or kNoJump once the subtree is done.
    std::size_t run(const std::vector<int>& colour) {
        const std::size_t n = colour.size();
        std::vector<std::vector<int>> cells(n);
        for (std::size_t v = 0; v < n; ++v)
            cells[std::size_t(colour[v])].push_back(int(v));
        auto open = std::find_if(cells.begin(), cells.end(),
                                 [](const std::vector<int>& c) { return c.size() > 1; });
        if (open == cells.end())
            return leaf(colour);
        // Which vertex represents a twin class depends on vertex order, but every skipped vertex
        // is a twin of an earlier one, so the minimum over the explored leaves does not.
        std::vector<int> representatives;
        for (int v : *open) {
            bool covered = std::any_of(representatives.begin(), representatives.end(),
                                       [&](int r) { return twins(g, r, v); });
            if (!covered)
                representatives.push_back(v);
        }
        const std::size_t depth = path.size();
        std::vector<int> done;
        std::vector<int> orbit(n, -1);
        std::size_t orbit_generators = 0;
        for (int v : representatives) {
            // Recomputed whenever a child's subtree found new automorphisms: they can merge a
            // later child into the orbit of an earlier one.
            if (!done.empty() && !group.empty() && orbit_generators != group.generators()) {
                std::vector<std::vector<int>> orbits = group.orbits_fixing(path, *open);
                for (std::size_t k = 0; k < orbits.size(); ++k)
                    for (int x : orbits[k])
                        orbit[std::size_t(x)] = int(k);
                orbit_generators = group.generators();
            }
            if (orbit_generators > 0 && std::any_of(done.begin(), done.end(), [&](int d) {
                    return orbit[std::size_t(d)] == orbit[std::size_t(v)];
                }))
                continue;
            std::vector<int> next(n);
            for (std::size_t x = 0; x < n; ++x)
                next[x] = 2 * colour[x] + 1;
            next[std::size_t(v)] = 2 * colour[std::size_t(v)];
            path.push_back(v);
            const std::size_t jump = run(refine(g, std::move(next)));
            path.pop_back();
            if (jump != kNoJump && jump < depth)
                return jump;
            done.push_back(v);
        }
        return kNoJump;
    }

    std::size_t leaf(const std::vector<int>& colour) {
        if (++leaves > kMaxLeaves)
            throw TopologyError("leaf_cap", "",
                                "canonical search exceeded " + std::to_string(kMaxLeaves) +
                                    " leaves");
        std::string cert = certificate(g, colour);
        if (!found) {
            first = {cert, colour, path};
            best = {std::move(cert), colour, path};
            found = true;
            return kNoJump;
        }
        std::size_t jump = kNoJump;
        const Leaf* stored_leaves[] = {&first, &best};
        for (const Leaf* stored : stored_leaves) {
            if (cert != stored->cert)
                continue;
            // γ = π_stored⁻¹ ∘ π_new sends each vertex here to the vertex at its position there.
            const Perm at = inverse(stored->position);
            Perm gamma(colour.size());
            for (std::size_t x = 0; x < colour.size(); ++x)
                gamma[x] = at[std::size_t(colour[x])];
            assert(is_automorphism(g, gamma));
            group.add(gamma);
            std::size_t d = 0;
            while (d < path.size() && d < stored->path.size() && path[d] == stored->path[d])
                ++d;
            jump = std::min(jump, d);
        }
        if (cert < best.cert)
            best = {std::move(cert), colour, path};
        return jump;
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
    Search search(g);
    search.run(refine(g, rank_values(g.vkey)));
    return search.best.cert;
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
