#include "core/writers.hpp"

#include <algorithm>
#include <array>
#include <cctype>
#include <charconv>
#include <cstdint>
#include <cstdio>
#include <tuple>
#include <type_traits>
#include <utility>
#include <variant>

namespace ostia::fabric::topology::capture {

namespace {

using nlohmann::json;

constexpr std::uint32_t kMaxDomain = 0xffff;

// One field of a bus ID: exactly `digits` hex digits, or 1-8 when digits is 0 (the domain).
std::optional<std::uint32_t> hex_field(std::string_view text, std::size_t digits) {
    if (text.empty() || (digits != 0 && text.size() != digits) || text.size() > 8) {
        return std::nullopt;
    }
    std::uint32_t value = 0;
    const char* const last = text.data() + text.size();
    const auto [ptr, ec] = std::from_chars(text.data(), last, value, 16);
    if (ec != std::errc{} || ptr != last) {
        return std::nullopt;
    }
    return value;
}

std::string link_layer(std::string text) {
    for (char& c : text) {
        c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    }
    if (text != "ethernet" && text != "infiniband") {
        return "unknown";
    }
    return text;
}

json speed_value(const std::variant<int, std::string>& value) {
    return std::visit([](const auto& v) { return json(v); }, value);
}

json link_json(const NvLink& link) {
    json j = {{"link", link.link},
              {"state", link.state},
              {"version", link.version},
              {"remote_type", link.remote_type}};
    if (link.remote_bus_id) {
        j["remote_bus_id"] = *link.remote_bus_id;
    }
    return j;
}

std::string or_unknown(const std::string& text) { return text.empty() ? "unknown" : text; }

} // namespace

Result<std::string> normalize_bus_id(std::string_view text) {
    const std::size_t colon1 = text.find(':');
    const std::size_t colon2 =
        colon1 == std::string_view::npos ? colon1 : text.find(':', colon1 + 1);
    const std::size_t dot = colon2 == std::string_view::npos ? colon2 : text.find('.', colon2 + 1);
    if (dot == std::string_view::npos) {
        return Result<std::string>::failure("bus_id_format");
    }
    const std::optional<std::uint32_t> domain = hex_field(text.substr(0, colon1), 0);
    const std::optional<std::uint32_t> bus =
        hex_field(text.substr(colon1 + 1, colon2 - colon1 - 1), 2);
    const std::optional<std::uint32_t> dev =
        hex_field(text.substr(colon2 + 1, dot - colon2 - 1), 2);
    const std::optional<std::uint32_t> func = hex_field(text.substr(dot + 1), 1);
    if (!domain || !bus || !dev || !func || *func > 7) {
        return Result<std::string>::failure("bus_id_format");
    }
    if (*domain > kMaxDomain) {
        return Result<std::string>::failure("bus_id_domain");
    }
    std::array<char, 16> buf{};
    std::snprintf(buf.data(), buf.size(), "%04x:%02x:%02x.%01x", *domain, *bus, *dev, *func);
    return std::string(buf.data());
}

json nvml_json(const NvmlFacts& facts) {
    std::vector<const NvmlGpu*> gpus;
    gpus.reserve(facts.gpus.size());
    for (const NvmlGpu& g : facts.gpus) {
        gpus.push_back(&g);
    }
    std::ranges::sort(gpus, {}, [](const NvmlGpu* g) { return g->bus_id; });

    json out_gpus = json::array();
    int ordinal = 0;
    for (const NvmlGpu* g : gpus) {
        json links;
        if (const auto* list = std::get_if<std::vector<NvLink>>(&g->nvlinks)) {
            links = json::array();
            for (const NvLink& link : *list) {
                links.push_back(link_json(link));
            }
        } else {
            links = "not_supported";
        }
        out_gpus.push_back({{"bus_id", g->bus_id},
                            {"name", g->name},
                            {"cc_major", g->cc_major},
                            {"cc_minor", g->cc_minor},
                            {"memory_bytes", g->memory_bytes},
                            {"cuda_ordinal", ordinal++},
                            {"nvlinks", std::move(links)}});
    }

    std::vector<P2p> p2p = facts.p2p;
    std::ranges::sort(
        p2p, [](const P2p& x, const P2p& y) { return std::tie(x.a, x.b) < std::tie(y.a, y.b); });
    json out_p2p = json::array();
    for (const P2p& p : p2p) {
        out_p2p.push_back({{"a", p.a},
                           {"b", p.b},
                           {"read", p.read},
                           {"write", p.write},
                           {"nvlink", p.nvlink},
                           {"atomics", p.atomics}});
    }
    return {{"schema", 1}, {"gpus", std::move(out_gpus)}, {"p2p", std::move(out_p2p)}};
}

json nics_json(const std::vector<NicFacts>& nics,
               const std::optional<std::vector<VerbsPort>>& ports) {
    std::vector<NicFacts> sorted = nics;
    std::ranges::sort(sorted, {}, &NicFacts::bus_id);
    json out_nics = json::array();
    for (const NicFacts& n : sorted) {
        out_nics.push_back({{"bus_id", n.bus_id},
                            {"driver", n.driver},
                            {"link_layer", link_layer(n.link_layer)},
                            {"max_pcie_gen", n.max_gen},
                            {"max_pcie_width", n.max_width},
                            {"numa_node", n.numa},
                            {"port_speed_mbps", speed_value(n.port_speed_mbps)}});
    }

    json rdma = json::array();
    if (ports) {
        std::vector<VerbsPort> sorted_ports = *ports;
        std::ranges::sort(sorted_ports, [](const VerbsPort& x, const VerbsPort& y) {
            return std::tie(x.bus_id, x.device, x.port) < std::tie(y.bus_id, y.device, y.port);
        });
        for (const VerbsPort& p : sorted_ports) {
            rdma.push_back({{"bus_id", p.bus_id},
                            {"device", p.device},
                            {"port", p.port},
                            {"state", p.state},
                            {"link_layer", link_layer(p.link_layer)},
                            {"active_speed", speed_value(p.active_speed)},
                            {"active_width", speed_value(p.active_width)},
                            {"gpudirect", p.gpudirect}});
        }
    }
    return {{"schema", 1},
            {"rdma_probe", ports ? "ok" : "unavailable"},
            {"nics", std::move(out_nics)},
            {"rdma", std::move(rdma)}};
}

json meta_json(const MetaFlags& flags, const NvmlFacts* nvml, std::string_view captured_at) {
    return {{"schema", 1},
            {"provider", flags.provider},
            {"instance_type", flags.instance_type},
            {"driver_version", nvml != nullptr ? or_unknown(nvml->driver) : "unknown"},
            {"cuda_version", nvml != nullptr ? or_unknown(nvml->cuda) : "unknown"},
            {"tool_version", OSTIA_TOOL_VERSION},
            {"captured_at", std::string(captured_at)}};
}

std::string dump(const json& doc) {
    std::string text = doc.dump(2);
    text += '\n';
    return text;
}

} // namespace ostia::fabric::topology::capture
