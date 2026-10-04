#include "core/nics.hpp"

#include <array>
#include <cctype>
#include <charconv>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <initializer_list>
#include <optional>
#include <string>
#include <utility>

namespace ostia::fabric::topology::capture {

namespace {

constexpr std::uint32_t kClassEthernet = 0x0200;
constexpr std::uint32_t kClassInfiniband = 0x0207;
constexpr std::string_view kPciDevices = "bus/pci/devices/";

struct GenEntry {
    std::string_view text;
    int gen;
};
constexpr std::array<GenEntry, 6> kGenTable{{
    {.text = "2.5", .gen = 1},
    {.text = "5.0", .gen = 2},
    {.text = "8.0", .gen = 3},
    {.text = "16.0", .gen = 4},
    {.text = "32.0", .gen = 5},
    {.text = "64.0", .gen = 6},
}};

struct PortFacts {
    std::variant<int, std::string> speed_mbps;
    std::string link_layer;
};

std::string join(std::initializer_list<std::string_view> parts) {
    std::string out;
    for (const std::string_view part : parts) {
        out += part;
    }
    return out;
}

std::optional<std::uint32_t> parse_hex(const std::string& text) {
    if (text.empty()) {
        return std::nullopt;
    }
    char* end = nullptr;
    const unsigned long value = std::strtoul(text.c_str(), &end, 16);
    if (end == text.c_str() || *end != '\0') {
        return std::nullopt;
    }
    return static_cast<std::uint32_t>(value);
}

std::optional<int> parse_int(const std::string& text) {
    int value = 0;
    const char* const first = text.data();
    const char* const last = first + text.size();
    const auto [ptr, ec] = std::from_chars(first, last, value);
    if (ec != std::errc{} || ptr != last) {
        return std::nullopt;
    }
    return value;
}

std::string lowered(std::string text) {
    for (char& c : text) {
        c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    }
    return text;
}

// "400 Gb/sec (4X NDR)" -> 400000 Mb/s.
std::optional<int> parse_rate_mbps(const std::string& text) {
    char* end = nullptr;
    const double gbps = std::strtod(text.c_str(), &end);
    if (end == text.c_str() || gbps <= 0.0) {
        return std::nullopt;
    }
    return static_cast<int>(std::llround(gbps * 1000.0));
}

std::string link_layer_from_type(const std::string& type) {
    if (type == "1") {
        return "ethernet";
    }
    if (type == "32") {
        return "infiniband";
    }
    return "unknown";
}

std::optional<PortFacts> net_facts(const SysfsReader& sysfs, const std::string& dev) {
    const std::string net = join({kPciDevices, dev, "/net"});
    std::optional<PortFacts> down;
    for (const std::string& ifname : sysfs.list(net)) {
        const std::optional<std::string> speed = sysfs.read(join({net, "/", ifname, "/speed"}));
        const std::optional<std::string> type = sysfs.read(join({net, "/", ifname, "/type"}));
        const std::optional<int> mbps = speed ? parse_int(*speed) : std::nullopt;
        std::string layer = type ? link_layer_from_type(*type) : "unknown";
        // A link that is down reports -1: the speed is unknown but the type still names the layer.
        if (mbps && *mbps > 0) {
            return PortFacts{.speed_mbps = *mbps, .link_layer = std::move(layer)};
        }
        if (!down && layer != "unknown") {
            down = PortFacts{.speed_mbps = std::string("unknown"), .link_layer = std::move(layer)};
        }
    }
    return down;
}

std::optional<PortFacts> ib_facts(const SysfsReader& sysfs, const std::string& dev) {
    const std::string ib = join({kPciDevices, dev, "/infiniband"});
    for (const std::string& ibdev : sysfs.list(ib)) {
        const std::string ports = join({ib, "/", ibdev, "/ports"});
        for (const std::string& port : sysfs.list(ports)) {
            const std::optional<std::string> rate = sysfs.read(join({ports, "/", port, "/rate"}));
            const std::optional<std::string> layer =
                sysfs.read(join({ports, "/", port, "/link_layer"}));
            const std::optional<int> mbps = rate ? parse_rate_mbps(*rate) : std::nullopt;
            if (mbps) {
                return PortFacts{.speed_mbps = *mbps,
                                 .link_layer = layer ? lowered(*layer) : "unknown"};
            }
        }
    }
    return std::nullopt;
}

struct CodeValue {
    int code;
    int value;
};
// ibverbs active_speed codes: per-lane Mb/s.
constexpr std::array<CodeValue, 9> kVerbsSpeeds{{{.code = 1, .value = 2500},
                                                 {.code = 2, .value = 5000},
                                                 {.code = 4, .value = 10000},
                                                 {.code = 8, .value = 10000},
                                                 {.code = 16, .value = 14000},
                                                 {.code = 32, .value = 25000},
                                                 {.code = 64, .value = 50000},
                                                 {.code = 128, .value = 100000},
                                                 {.code = 256, .value = 200000}}};
// ibverbs active_width codes: lane counts.
constexpr std::array<CodeValue, 5> kVerbsWidths{{{.code = 1, .value = 1},
                                                 {.code = 2, .value = 4},
                                                 {.code = 4, .value = 8},
                                                 {.code = 8, .value = 12},
                                                 {.code = 16, .value = 2}}};

template <std::size_t N>
std::optional<int> lookup(const std::array<CodeValue, N>& table,
                          const std::variant<int, std::string>& code) {
    const int* value = std::get_if<int>(&code);
    if (value == nullptr) {
        return std::nullopt;
    }
    for (const CodeValue& entry : table) {
        if (entry.code == *value) {
            return entry.value;
        }
    }
    return std::nullopt;
}

std::optional<PortFacts> verbs_facts(const std::vector<VerbsPort>& verbs, const std::string& dev) {
    for (const VerbsPort& port : verbs) {
        if (port.bus_id != dev || port.link_layer.empty()) {
            continue;
        }
        const std::optional<int> lane = lookup(kVerbsSpeeds, port.active_speed);
        const std::optional<int> lanes = lookup(kVerbsWidths, port.active_width);
        std::variant<int, std::string> speed = std::string("unknown");
        if (lane && lanes) {
            speed = *lane * *lanes;
        }
        return PortFacts{.speed_mbps = std::move(speed), .link_layer = lowered(port.link_layer)};
    }
    return std::nullopt;
}

} // namespace

int parse_pcie_gen(std::string_view text) {
    for (const GenEntry& entry : kGenTable) {
        if (!text.starts_with(entry.text)) {
            continue;
        }
        std::string_view rest = text.substr(entry.text.size());
        if (rest.starts_with(" GT/s")) {
            rest.remove_prefix(5);
            if (rest.empty() || rest == " PCIe") {
                return entry.gen;
            }
        }
    }
    return 0;
}

std::vector<NicFacts> scan_nics(const SysfsReader& sysfs, const std::vector<VerbsPort>& verbs,
                                Diagnostics& diag) {
    std::vector<NicFacts> nics;
    std::size_t skipped_vfs = 0;
    for (const std::string& dev : sysfs.list(kPciDevices)) {
        const std::string base = join({kPciDevices, dev});
        const std::optional<std::string> cls_text = sysfs.read(join({base, "/class"}));
        const std::optional<std::uint32_t> cls = cls_text ? parse_hex(*cls_text) : std::nullopt;
        if (!cls) {
            continue;
        }
        const std::uint32_t top = *cls >> 8;
        if (top != kClassEthernet && top != kClassInfiniband) {
            continue;
        }
        if (sysfs.link_name(join({base, "/physfn"}))) {
            ++skipped_vfs;
            continue;
        }

        NicFacts nic{.bus_id = dev,
                     .driver = sysfs.link_name(join({base, "/driver"})).value_or("unknown"),
                     .link_layer = "unknown",
                     .max_gen = 0,
                     .max_width = 0,
                     .numa = -1,
                     .port_speed_mbps = std::string("unknown")};
        const std::optional<std::string> speed = sysfs.read(join({base, "/max_link_speed"}));
        if (speed) {
            nic.max_gen = parse_pcie_gen(*speed);
            if (nic.max_gen == 0) {
                diag.add(join({"nic ", dev, ": unrecognised max_link_speed"}));
            }
        }
        const std::optional<std::string> width = sysfs.read(join({base, "/max_link_width"}));
        nic.max_width = width ? parse_int(*width).value_or(0) : 0;
        const std::optional<std::string> numa = sysfs.read(join({base, "/numa_node"}));
        nic.numa = numa ? parse_int(*numa).value_or(-1) : -1;

        // The first source with a known speed wins; a source that only knows the layer (a down
        // link, an IPoIB netdev reporting -1) supplies the layer when no source has a speed.
        const std::array<std::optional<PortFacts>, 3> sources{
            net_facts(sysfs, dev), ib_facts(sysfs, dev), verbs_facts(verbs, dev)};
        const PortFacts* chosen = nullptr;
        for (const std::optional<PortFacts>& source : sources) {
            if (source && std::holds_alternative<int>(source->speed_mbps)) {
                chosen = &*source;
                break;
            }
        }
        if (chosen != nullptr) {
            nic.port_speed_mbps = chosen->speed_mbps;
        }
        if (chosen != nullptr && chosen->link_layer != "unknown") {
            nic.link_layer = chosen->link_layer;
        } else {
            for (const std::optional<PortFacts>& source : sources) {
                if (source && source->link_layer != "unknown") {
                    nic.link_layer = source->link_layer;
                    break;
                }
            }
        }
        if (std::holds_alternative<std::string>(nic.port_speed_mbps)) {
            diag.add(
                join({"nic ", dev, ": port facts unknown (host network namespace not visible)"}));
        }
        nics.push_back(std::move(nic));
    }
    if (skipped_vfs > 0) {
        diag.add(join({"skipped ", std::to_string(skipped_vfs), " SR-IOV virtual function(s)"}));
    }
    return nics;
}

} // namespace ostia::fabric::topology::capture
