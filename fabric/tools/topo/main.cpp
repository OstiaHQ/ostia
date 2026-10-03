#include <algorithm>
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

namespace fs = std::filesystem;
using namespace ostia::fabric::topology;

namespace {

constexpr int kOk = 0, kMismatch = 1, kUsage = 2, kTopology = 3;
constexpr std::size_t kMaxDiffLines = 40;

constexpr std::string_view kUsageText =
    "usage: ostia-topo show <dir>\n"
    "       ostia-topo model <dir> [--check <expected.json>]\n"
    "       ostia-topo id <dir>\n"
    "       ostia-topo --help\n"
    "<dir> is a fixture directory, or a pair directory holding pair.json and node dirs.\n";

struct Usage {};

bool is_pair(const fs::path& dir) { return fs::exists(dir / "pair.json"); }

nlohmann::json read_pair(const fs::path& dir) {
    const fs::path path = dir / "pair.json";
    std::ifstream in(path);
    if (!in) {
        throw TopologyError("io", path.string(), "cannot read file");
    }
    auto j = nlohmann::json::parse(in, nullptr, false);
    if (j.is_discarded() || !j.is_object() || !j.contains("nodes") || !j["nodes"].is_array() ||
        j["nodes"].size() != 2) {
        throw TopologyError("schema", path.string(), "pair.json needs a two-entry nodes array");
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
// change in place and the first differing lines are what the reader needs.
void print_diff(const std::string& expected, const std::string& actual) {
    const auto e = lines_of(expected), a = lines_of(actual);
    std::cout << "--- expected\n+++ actual\n";
    std::size_t shown = 0;
    for (std::size_t i = 0; i < std::max(e.size(), a.size()) && shown < kMaxDiffLines; ++i) {
        const bool has_e = i < e.size(), has_a = i < a.size();
        if (has_e && has_a && e[i] == a[i]) {
            continue;
        }
        std::cout << "@@ line " << i + 1 << " @@\n";
        if (has_e) {
            std::cout << "-" << e[i] << "\n";
            ++shown;
        }
        if (has_a && shown < kMaxDiffLines) {
            std::cout << "+" << a[i] << "\n";
            ++shown;
        }
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
        std::sort(v.begin(), v.end(), [](auto* x, auto* y) { return x->key < y->key; });
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
    std::int64_t switch_links = 0;
    for (const auto& e : model.edges) {
        if (e.kind != EdgeKind::nvlink) {
            continue;
        }
        if (e.to == "switch-group-0") {
            ++to_switch;
            switch_links = e.attrs.at("links");
        } else {
            ++direct;
        }
    }
    if (to_switch != 0) {
        std::cout << "  NVLink: " << to_switch << " GPUs -> switch-group-0, " << switch_links
                  << " links each\n";
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
    print_diff(expected, actual);
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

std::string fix_for(const std::string& code) {
    if (code == "schema") {
        return "regenerate with fabric/tests/fixtures/topology/synthetic/generate.py, or recapture";
    }
    if (code == "dangling_reference") {
        return "the bus ID must appear in hwloc.xml; recapture";
    }
    if (code == "xml") {
        return "recapture; hwloc >= 2.4 must import it";
    }
    return "inspect the named fixture file and recapture it";
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
    const std::vector<std::string_view> args(argv + 1, argv + argc);
    try {
        return run(args);
    } catch (const Usage&) {
        std::cerr << kUsageText;
        return kUsage;
    } catch (const TopologyError& e) {
        return report(e);
    }
}
