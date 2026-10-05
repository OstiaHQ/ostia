#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <iterator>
#include <map>
#include <nlohmann/json.hpp>
#include <set>
#include <sstream>
#include <string>
#include <string_view>
#include <vector>

#include "topology/builder.hpp"
#include "topology/fixture_source.hpp"
#include "topology/identity.hpp"
#include "topology/schema.hpp"

namespace fs = std::filesystem;
using namespace ostia::fabric::topology;

namespace {

// 3: the fixture could not be read or replayed.
constexpr int kOk = 0, kMismatch = 1, kUsage = 2, kTopology = 3;
constexpr std::size_t kMaxDiffLines = 40;

constexpr std::string_view kUsageText =
    "usage: ostia-topo show <dir>\n"
    "       ostia-topo model <dir> [--check <expected.json>]\n"
    "       ostia-topo id <dir>\n"
    "       ostia-topo diff <a> <b>\n"
    "       ostia-topo --help\n"
    "<dir> is a fixture directory, or a pair directory holding pair.json and node dirs.\n"
    "diff exits 0 when the topology ids are equal and 1 when they differ.\n";

struct Usage {};

bool is_pair(const fs::path& dir) { return fs::exists(dir / "pair.json"); }

nlohmann::json read_pair(const fs::path& dir) {
    const fs::path path = dir / "pair.json";
    std::ifstream in(path);
    if (!in) {
        throw TopologyError("io", path.string(), "cannot read file");
    }
    auto j = nlohmann::json::parse(in, nullptr, false);
    if (j.is_discarded()) {
        throw TopologyError("schema", path.string(), "not valid JSON");
    }
    if (const auto errors = validate("pair", j); !errors.empty()) {
        throw TopologyError("schema", path.string(),
                            errors.front().path + ": " + errors.front().message);
    }
    if (j["nodes"].size() != 2 || !j["nodes"][0].is_string() || !j["nodes"][1].is_string()) {
        throw TopologyError("schema", path.string(), "nodes must be two directory names");
    }
    return j;
}

Model load(const fs::path& dir) { return build(FixtureSource(dir).facts()); }

std::string pair_id_of(const nlohmann::json& pair, const Model& m0, const Model& m1) {
    return pair_id(topo1(m0), topo1(m1), pair);
}

std::string model_text(const fs::path& dir) {
    if (!is_pair(dir)) {
        return canonical_dump(load(dir));
    }
    const auto pair = read_pair(dir);
    const Model m0 = load(dir / pair["nodes"][0].get<std::string>());
    const Model m1 = load(dir / pair["nodes"][1].get<std::string>());
    nlohmann::json out;
    out["nodes"] = {to_json(m0), to_json(m1)};
    out["pair_id"] = pair_id_of(pair, m0, m1);
    return out.dump(2) + "\n";
}

std::vector<std::string> lines_of(const std::string& text) {
    std::vector<std::string> lines;
    std::istringstream in(text);
    for (std::string line; std::getline(in, line);) {
        lines.push_back(line);
    }
    return lines;
}

// Positional, not an LCS diff: goldens are canonical JSON, so a mismatch is usually a value
// change in place. The line counts expose an insertion or deletion, after which the rest of the
// file is misaligned. Every printed line counts toward the cap.
void print_diff(const std::string& expected, const std::string& actual, std::string_view minus,
                std::string_view plus) {
    const auto e = lines_of(expected), a = lines_of(actual);
    const std::size_t n = std::max(e.size(), a.size());
    std::vector<std::size_t> differing;
    for (std::size_t i = 0; i < n; ++i) {
        if (i >= e.size() || i >= a.size() || e[i] != a[i]) {
            differing.push_back(i);
        }
    }
    std::cout << "--- " << minus << " (" << e.size() << " lines)\n+++ " << plus << " (" << a.size()
              << " lines)\n";
    std::size_t printed = 2;
    std::size_t consumed = 0;
    for (std::size_t k = 0; k < differing.size(); ++k) {
        const std::size_t i = differing[k];
        const bool run_start = k == 0 || differing[k - 1] + 1 != i;
        const std::size_t cost =
            (run_start ? 1 : 0) + (i < e.size() ? 1 : 0) + (i < a.size() ? 1 : 0);
        if (printed + cost > kMaxDiffLines - 1) {
            break;
        }
        if (run_start) {
            std::cout << "@@ line " << i + 1 << " @@\n";
        }
        if (i < e.size()) {
            std::cout << "-" << e[i] << "\n";
        }
        if (i < a.size()) {
            std::cout << "+" << a[i] << "\n";
        }
        printed += cost;
        consumed = k + 1;
    }
    if (consumed < differing.size()) {
        std::cout << "... " << differing.size() - consumed << " more differing lines\n";
    }
}

std::string attr_str(const Node& n, const char* key) {
    auto it = n.attrs.find(key);
    if (it == n.attrs.end()) {
        return "?";
    }
    return std::visit(
        [](const auto& v) {
            std::ostringstream s;
            s << v;
            return s.str();
        },
        it->second);
}

void show_node(const std::string& title, const fs::path& dir) {
    const auto facts = FixtureSource(dir).facts();
    const Model model = build(facts);

    std::map<std::string, std::vector<const Node*>> by_kind;
    for (const auto& n : model.nodes) {
        by_kind[std::string(to_string(n.kind))].push_back(&n);
    }
    auto sorted = [&](const char* kind) {
        auto v = by_kind[kind];
        std::ranges::sort(v, [](auto* x, auto* y) { return x->key < y->key; });
        return v;
    };
    auto numa_of = [&](const std::string& key) {
        for (const auto& e : model.edges) {
            if (e.kind == EdgeKind::numa_local && e.from == key) {
                return e.to.substr(e.to.find('-') + 1);
            }
        }
        return std::string("?");
    };

    const std::string id = topo1(model);
    std::cout << title << "  " << id.substr(0, id.rfind(':') + 1 + 12) << "\n";
    std::cout << "  " << facts.packages << " packages, " << facts.numa_nodes.size()
              << " NUMA nodes\n";

    const auto gpus = sorted("gpu");
    std::cout << "  GPUs (" << gpus.size() << ")";
    if (!gpus.empty()) {
        const Node& g = *gpus.front();
        std::cout << ": " << attr_str(g, "model") << " (cc " << attr_str(g, "cc_major") << "."
                  << attr_str(g, "cc_minor") << ")  " << g.key << " ... numa " << numa_of(g.key);
    }
    std::cout << "\n";

    std::set<std::string> gpu_keys;
    for (auto* g : gpus) {
        gpu_keys.insert(g->key);
    }
    std::size_t to_switch = 0, direct = 0;
    std::int64_t min_links = 0, max_links = 0;
    for (const auto& e : model.edges) {
        if (e.kind != EdgeKind::nvlink) {
            continue;
        }
        if (e.to == "switch-group-0") {
            const std::int64_t links = e.attrs.at("links");
            min_links = to_switch == 0 ? links : std::min(min_links, links);
            max_links = to_switch == 0 ? links : std::max(max_links, links);
            ++to_switch;
        } else {
            ++direct;
        }
    }
    if (to_switch != 0) {
        std::cout << "  NVLink: " << to_switch << " GPUs -> switch-group-0, ";
        if (min_links == max_links) {
            std::cout << min_links << " links each\n";
        } else {
            std::cout << min_links << "-" << max_links << " links\n";
        }
    } else if (direct != 0) {
        std::cout << "  NVLink: " << direct << " direct GPU pairs\n";
    } else {
        std::cout << "  NVLink: none\n";
    }

    const auto nics = sorted("nic");
    std::cout << "  NICs (" << nics.size() << ")";
    if (!nics.empty()) {
        const Node& n = *nics.front();
        std::cout << ": " << attr_str(n, "driver") << " " << attr_str(n, "link_layer") << "  "
                  << n.key << " numa " << attr_str(n, "numa_node");
        if (attr_str(n, "port_speed_mbps") == "unknown") {
            std::cout << "  [port speed unknown]";
        }
    }
    std::cout << "\n";

    std::cout << "  PCIe: " << by_kind["pcie_bridge"].size() << " bridges";
    for (const auto& e : model.edges) {
        if (e.kind == EdgeKind::pcie && gpu_keys.count(e.to) && e.attrs.count("gen")) {
            std::cout << "; GPU links gen " << e.attrs.at("gen") << " x"
                      << (e.attrs.count("width") ? e.attrs.at("width") : 0);
            break;
        }
    }
    std::cout << "\n";

    if (!facts.links.is_object() || !facts.links.contains("links") ||
        facts.links["links"].empty()) {
        std::cout << "  Measured links: none (no links.json)\n";
        return;
    }
    std::cout << "  Measured links:\n";
    for (const auto& l : facts.links["links"]) {
        std::cout << "    " << l["from"].get<std::string>() << " -> " << l["to"].get<std::string>()
                  << "  " << l["bw_mbps"] << " MB/s (test bench)\n";
    }
}

int cmd_show(const fs::path& dir) {
    if (!is_pair(dir)) {
        show_node(dir.filename().string(), dir);
        return kOk;
    }
    const auto pair = read_pair(dir);
    for (const auto& n : pair["nodes"]) {
        show_node(dir.filename().string() + "/" + n.get<std::string>(), dir / n.get<std::string>());
    }
    const Model m0 = load(dir / pair["nodes"][0].get<std::string>());
    const Model m1 = load(dir / pair["nodes"][1].get<std::string>());
    std::cout << "pair  " << pair_id_of(pair, m0, m1) << "\n";
    return kOk;
}

int cmd_model(const fs::path& dir, const char* check) {
    const std::string actual = model_text(dir);
    if (check == nullptr) {
        std::cout << actual;
        return kOk;
    }
    std::ifstream in(check, std::ios::binary);
    if (!in) {
        throw TopologyError("io", check, "cannot read expected file");
    }
    const std::string expected((std::istreambuf_iterator<char>(in)), {});
    if (expected == actual) {
        return kOk;
    }
    print_diff(expected, actual, "expected", "actual");
    return kMismatch;
}

int cmd_id(const fs::path& dir) {
    if (!is_pair(dir)) {
        std::cout << topo1(load(dir)) << "\n";
        return kOk;
    }
    const auto pair = read_pair(dir);
    const Model m0 = load(dir / pair["nodes"][0].get<std::string>());
    const Model m1 = load(dir / pair["nodes"][1].get<std::string>());
    std::cout << pair_id_of(pair, m0, m1) << "\n";
    return kOk;
}

// Object members recurse under their key; every array element is one line. A certificate's
// nodes and edges are array elements, so a positional diff names the node or edge that changed.
void outline(const nlohmann::json& j, const std::string& indent, std::string& out) {
    if (j.is_object()) {
        for (const auto& [key, value] : j.items()) {
            if (value.is_structured()) {
                out += indent + key + ":\n";
                outline(value, indent + "  ", out);
            } else {
                out += indent + key + ": " + value.dump() + "\n";
            }
        }
        return;
    }
    if (j.is_array()) {
        for (const auto& element : j) {
            out += indent + element.dump() + "\n";
        }
        return;
    }
    out += indent + j.dump() + "\n";
}

// What `diff` compares for one input: the identity (key-free, RFC-0003 §5) and the keyed model.
struct Side {
    std::string id;
    std::string identity;
    std::set<std::string> keys;
    std::string model;
};

void add_keys(const Model& m, const std::string& prefix, std::set<std::string>& keys) {
    for (const auto& n : m.nodes) {
        keys.insert(prefix + n.key);
    }
}

Side side_of(const fs::path& dir) {
    Side side;
    if (!is_pair(dir)) {
        const Model m = load(dir);
        side.id = topo1(m);
        outline(nlohmann::json::parse(canonical_identity_json(m)), "", side.identity);
        add_keys(m, "", side.keys);
        side.model = canonical_dump(m);
        return side;
    }
    const auto pair = read_pair(dir);
    const auto name0 = pair["nodes"][0].get<std::string>();
    const auto name1 = pair["nodes"][1].get<std::string>();
    const Model m0 = load(dir / name0);
    const Model m1 = load(dir / name1);
    side.id = pair_id_of(pair, m0, m1);
    nlohmann::json identity;
    identity["link_class"] = pair.contains("link_class") ? pair["link_class"] : nlohmann::json();
    identity["rails"] = pair.contains("rails") ? pair["rails"] : nlohmann::json();
    identity[name0] = nlohmann::json::parse(canonical_identity_json(m0));
    identity[name1] = nlohmann::json::parse(canonical_identity_json(m1));
    outline(identity, "", side.identity);
    add_keys(m0, name0 + "/", side.keys);
    add_keys(m1, name1 + "/", side.keys);
    side.model = model_text(dir);
    return side;
}

// Exit 0 iff the ids are equal. The identity diff comes first because it is what the id hashes;
// the keyed model diff follows only when both sides use the same bus IDs, since otherwise every
// line would differ by key alone.
int cmd_diff(const fs::path& a, const fs::path& b) {
    if (is_pair(a) != is_pair(b)) {
        throw TopologyError("diff_kinds", "", "cannot compare a pair with a single machine");
    }
    const Side sa = side_of(a);
    const Side sb = side_of(b);
    const bool same = sa.id == sb.id;
    std::cout << "a: " << sa.id << "\nb: " << sb.id << "\n"
              << (same ? "same topology\n" : "different topology\n");
    if (!same) {
        print_diff(sa.identity, sb.identity, "a identity", "b identity");
    }
    if (sa.keys == sb.keys && sa.model != sb.model) {
        std::cout << "keyed model (the bus IDs match):\n";
        print_diff(sa.model, sb.model, "a model", "b model");
    }
    return same ? kOk : kMismatch;
}

std::string fix_for(const std::string& code) {
    if (code == "diff_kinds") {
        return "pass two machine fixtures or two pair fixtures";
    }
    if (code == "schema") {
        return "regenerate with fabric/tests/fixtures/topology/synthetic/generate.py, or recapture";
    }
    if (code == "dangling_reference") {
        return "the bus ID must appear in hwloc.xml; recapture";
    }
    if (code == "xml") {
        return "recapture; hwloc >= 2.4 must import it";
    }
    return "inspect the named fixture file and recapture it; a path error means the directory is "
           "wrong";
}

int report(const TopologyError& e) {
    std::cerr << "error: " << e.what() << "\n"
              << "  rule: fixtures follow RFC-0003 §2 and §9\n"
              << "  fix: " << fix_for(e.code) << "\n"
              << "  see: RFC-0003 §9\n";
    return kTopology;
}

int run(const std::vector<std::string_view>& args) {
    if (args.empty()) {
        throw Usage{};
    }
    if (args[0] == "--help" || args[0] == "-h") {
        std::cout << kUsageText;
        return kOk;
    }
    if (args.size() < 2) {
        throw Usage{};
    }
    const fs::path dir{std::string(args[1])};
    if (args[0] == "diff" && args.size() == 3) {
        return cmd_diff(dir, fs::path{std::string(args[2])});
    }
    if (args[0] == "show" && args.size() == 2) {
        return cmd_show(dir);
    }
    if (args[0] == "id" && args.size() == 2) {
        return cmd_id(dir);
    }
    if (args[0] == "model") {
        if (args.size() == 2) {
            return cmd_model(dir, nullptr);
        }
        if (args.size() == 4 && args[2] == "--check") {
            const std::string path(args[3]);
            return cmd_model(dir, path.c_str());
        }
    }
    throw Usage{};
}

} // namespace

int main(int argc, char** argv) {
    try {
        const std::vector<std::string_view> args(argv + 1, argv + argc);
        try {
            return run(args);
        } catch (const Usage&) {
            std::cerr << kUsageText;
            return kUsage;
        } catch (const TopologyError& e) {
            return report(e);
        } catch (const std::exception& e) {
            return report(TopologyError("unreadable", "", e.what()));
        }
    } catch (...) {
        // Reporting the error threw as well, most likely out of memory: still exit non-zero.
        return kTopology;
    }
}
