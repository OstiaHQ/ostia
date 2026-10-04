#include "core/nics.hpp"

#include <array>
#include <cctype>
#include <charconv>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <optional>
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
    {"2.5", 1},
    {"5.0", 2},
    {"8.0", 3},
    {"16.0", 4},
    {"32.0", 5},
    {"64.0", 6},
}};

struct PortFacts {
    std::variant<int, std::string> speed_mbps;
    std::string link_layer;
};

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
    const std::string net = std::string(kPciDevices) + dev + "/net";
    for (const std::string& ifname : sysfs.list(net)) {
        const std::optional<std::string> speed = sysfs.read(net + "/" + ifname + "/speed");
        const std::optional<std::string> type = sysfs.read(net + "/" + ifname + "/type");
        const std::optional<int> mbps = speed ? parse_int(*speed) : std::nullopt;
        // A link that is down reports -1; treat it as no information, not as a speed.
        if (mbps && *mbps > 0) {
            return PortFacts{*mbps, type ? link_layer_from_type(*type) : "unknown"};
        }
    }
    return std::nullopt;
}

std::optional<PortFacts> ib_facts(const SysfsReader& sysfs, const std::string& dev) {
    const std::string ib = std::string(kPciDevices) + dev + "/infiniband";
    for (const std::string& ibdev : sysfs.list(ib)) {
        const std::string ports = ib + "/" + ibdev + "/ports";
        for (const std::string& port : sysfs.list(ports)) {
            const std::optional<std::string> rate = sysfs.read(ports + "/" + port + "/rate");
            const std::optional<std::string> layer = sysfs.read(ports + "/" + port + "/link_layer");
            const std::optional<int> mbps = rate ? parse_rate_mbps(*rate) : std::nullopt;
            if (mbps) {
                return PortFacts{*mbps, layer ? lowered(*layer) : "unknown"};
            }
        }
    }
    return std::nullopt;
}

// The verbs probe reports a speed code, not Mb/s, so only the link layer is taken from it.
std::optional<PortFacts> verbs_facts(const std::vector<VerbsPort>& verbs, const std::string& dev) {
    for (const VerbsPort& port : verbs) {
        if (port.bus_id == dev && !port.link_layer.empty()) {
            return PortFacts{std::string("unknown"), lowered(port.link_layer)};
        }
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
        const std::string base = std::string(kPciDevices) + dev;
        const std::optional<std::string> cls_text = sysfs.read(base + "/class");
        const std::optional<std::uint32_t> cls = cls_text ? parse_hex(*cls_text) : std::nullopt;
        if (!cls) {
            continue;
        }
        const std::uint32_t top = *cls >> 8;
        if (top != kClassEthernet && top != kClassInfiniband) {
            continue;
        }
        if (sysfs.link_name(base + "/physfn")) {
            ++skipped_vfs;
            continue;
        }

        NicFacts nic{dev,
                     sysfs.link_name(base + "/driver").value_or("unknown"),
                     "unknown",
                     0,
                     0,
                     -1,
                     std::string("unknown")};
        const std::optional<std::string> speed = sysfs.read(base + "/max_link_speed");
        if (speed) {
            nic.max_gen = parse_pcie_gen(*speed);
            if (nic.max_gen == 0) {
                diag.add("nic " + dev + ": unrecognised max_link_speed");
            }
        }
        const std::optional<std::string> width = sysfs.read(base + "/max_link_width");
        nic.max_width = width ? parse_int(*width).value_or(0) : 0;
        const std::optional<std::string> numa = sysfs.read(base + "/numa_node");
        nic.numa = numa ? parse_int(*numa).value_or(-1) : -1;

        std::optional<PortFacts> facts = net_facts(sysfs, dev);
        if (!facts) {
            facts = ib_facts(sysfs, dev);
        }
        if (!facts) {
            facts = verbs_facts(verbs, dev);
        }
        if (facts) {
            nic.port_speed_mbps = facts->speed_mbps;
            nic.link_layer = facts->link_layer;
        }
        if (!facts || std::holds_alternative<std::string>(nic.port_speed_mbps)) {
            diag.add("nic " + dev + ": port facts unknown (host network namespace not visible)");
        }
        nics.push_back(std::move(nic));
    }
    if (skipped_vfs > 0) {
        diag.add("skipped " + std::to_string(skipped_vfs) + " SR-IOV virtual function(s)");
    }
    return nics;
}

} // namespace ostia::fabric::topology::capture
