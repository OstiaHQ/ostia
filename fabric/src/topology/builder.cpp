#include "topology/builder.hpp"

#include <algorithm>
#include <map>
#include <set>

namespace ostia::fabric::topology {

namespace {

using nlohmann::json;

constexpr const char* kSwitchGroup = "switch-group-0"; // R7: one per machine

std::string numa_key(int os_index) { return "numa-" + std::to_string(os_index); }

std::int64_t integer(const json& j) { return j.get<std::int64_t>(); }

Value value_of(const json& j) {
    return j.is_string() ? Value{j.get<std::string>()} : Value{integer(j)};
}

} // namespace

Model build(const Facts& facts) {
    Model model;
    std::map<std::string, const PciFacts*> by_key;
    for (const auto& p : facts.pci) {
        by_key[p.key] = &p;
    }

    for (int i = 0; i < facts.packages; ++i) {
        model.nodes.push_back({NodeKind::package, "package-" + std::to_string(i), {}});
    }
    for (int n : facts.numa_nodes) {
        model.nodes.push_back({NodeKind::numa, numa_key(n), {}});
    }

    auto find_device = [&](const std::string& id, const char* file) -> const PciFacts& {
        auto it = by_key.find(id);
        if (it == by_key.end() || it->second->bridge) {
            throw TopologyError("dangling_reference", file,
                                "bus ID " + id + " has no PCI device in hwloc.xml");
        }
        return *it->second;
    };

    std::set<std::string> leaves;
    std::set<std::string> gpu_keys;

    if (facts.nvml.is_object()) {
        for (const auto& g : facts.nvml["gpus"]) {
            const std::string id = g["bus_id"];
            find_device(id, "nvml.json");
            Node node{NodeKind::gpu, id, {}};
            node.attrs["model"] = g["name"].get<std::string>();
            node.attrs["cc_major"] = integer(g["cc_major"]);
            node.attrs["cc_minor"] = integer(g["cc_minor"]);
            node.attrs["memory_bytes"] = integer(g["memory_bytes"]);
            node.attrs["cuda_ordinal"] = integer(g["cuda_ordinal"]);
            model.nodes.push_back(std::move(node));
            leaves.insert(id);
            gpu_keys.insert(id);
        }
    }

    std::set<std::string> rdma;
    for (const auto& r : facts.nics["rdma"]) {
        rdma.insert(r["bus_id"].get<std::string>());
    }
    for (const auto& n : facts.nics["nics"]) {
        const std::string id = n["bus_id"];
        const PciFacts& dev = find_device(id, "nics.json");
        Node node{NodeKind::nic, id, {}};
        node.attrs["pci_vendor"] = std::int64_t{dev.pci_vendor};
        node.attrs["pci_device"] = std::int64_t{dev.pci_device};
        node.attrs["driver"] = n["driver"].get<std::string>();
        node.attrs["link_layer"] = n["link_layer"].get<std::string>();
        node.attrs["numa_node"] = integer(n["numa_node"]);
        node.attrs["port_speed_mbps"] = value_of(n["port_speed_mbps"]);
        node.attrs["rdma_probe"] = facts.nics["rdma_probe"].get<std::string>();
        node.attrs["rdma"] = std::int64_t{rdma.count(id) ? 1 : 0};
        model.nodes.push_back(std::move(node));
        leaves.insert(id);
    }

    // Keep a bridge only while a gpu or nic lies below it.
    std::set<std::string> kept = leaves;
    for (const auto& leaf : leaves) {
        for (std::string k = by_key.at(leaf)->parent_key; !k.empty();
             k = by_key.at(k)->parent_key) {
            if (!kept.insert(k).second) {
                break;
            }
        }
    }

    for (const auto& p : facts.pci) {
        if (!kept.count(p.key)) {
            continue;
        }
        if (p.bridge) {
            model.nodes.push_back({NodeKind::pcie_bridge, p.key, {}});
        }
        if (!p.parent_key.empty()) {
            Edge e{EdgeKind::pcie, p.parent_key, p.key, {}};
            if (p.max_gen) {
                e.attrs["gen"] = p.max_gen;
            }
            if (p.max_width) {
                e.attrs["width"] = p.max_width;
            }
            model.edges.push_back(std::move(e));
        }
        if (leaves.count(p.key)) {
            for (int n : p.numa) {
                model.edges.push_back({EdgeKind::numa_local, p.key, numa_key(n), {}});
            }
        }
    }

    if (facts.nvml.is_object()) {
        std::map<std::pair<std::string, std::string>, std::int64_t> pairs;
        std::map<std::string, std::int64_t> to_switch;
        for (const auto& g : facts.nvml["gpus"]) {
            if (!g["nvlinks"].is_array()) {
                continue;
            }
            const std::string self = g["bus_id"];
            std::map<std::string, std::int64_t> peers;
            for (const auto& l : g["nvlinks"]) {
                if (l["state"] != "active") {
                    continue;
                }
                if (l["remote_type"] == "switch") {
                    ++to_switch[self];
                } else if (l["remote_type"] == "gpu") {
                    const std::string remote = l.value("remote_bus_id", "");
                    if (!gpu_keys.count(remote)) {
                        throw TopologyError("dangling_reference", "nvml.json",
                                            "nvlink remote " + remote + " of " + self +
                                                " is not a GPU in nvml.json");
                    }
                    ++peers[remote];
                }
            }
            for (const auto& [remote, count] : peers) {
                auto key = std::minmax(self, remote);
                auto& slot = pairs[{key.first, key.second}];
                slot = std::max(slot, count);
            }
        }
        for (const auto& [pair, count] : pairs) {
            model.edges.push_back({EdgeKind::nvlink, pair.first, pair.second, {{"links", count}}});
        }
        if (!to_switch.empty()) {
            model.nodes.push_back({NodeKind::switch_group, kSwitchGroup, {}});
            for (const auto& [gpu, count] : to_switch) {
                model.edges.push_back({EdgeKind::nvlink, gpu, kSwitchGroup, {{"links", count}}});
            }
        }
    }
    return model;
}

} // namespace ostia::fabric::topology
