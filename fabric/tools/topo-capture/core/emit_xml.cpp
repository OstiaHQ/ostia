#include "core/emit_xml.hpp"

#include <algorithm>
#include <array>
#include <charconv>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <map>
#include <memory>
#include <optional>
#include <string_view>
#include <utility>
#include <vector>

#include "core/nics.hpp"
#include "topology/error.hpp"

namespace ostia::fabric::topology::capture {

namespace {

// RFC-0003 §2.1. The two Ostia keys are not copied from the source: they are rewritten from
// the sysfs maxima.
constexpr std::array<std::string_view, 9> kInfoAllowlist{
    "PCIVendor",      "PCIDevice", "CPUVendor", "CPUModel", "CPUFamilyNumber",
    "CPUModelNumber", "GPUVendor", "GPUModel",  "Backend",
};

using PciAttr = hwloc_obj_attr_u::hwloc_pcidev_attr_s;

enum class Disposition : std::uint8_t { keep, splice, drop };

// An explicit allowlist, so an object type a newer hwloc adds is dropped rather than published.
// MemCache is spliced out (its NUMA node children stay); OS devices can carry MAC-derived names
// and Misc objects carry DMI memory-module serials, so both go with their subtrees.
Disposition disposition(hwloc_obj_type_t type) {
    switch (type) {
    case HWLOC_OBJ_MACHINE:
    case HWLOC_OBJ_PACKAGE:
    case HWLOC_OBJ_DIE:
    case HWLOC_OBJ_GROUP:
    case HWLOC_OBJ_NUMANODE:
    case HWLOC_OBJ_L1CACHE:
    case HWLOC_OBJ_L2CACHE:
    case HWLOC_OBJ_L3CACHE:
    case HWLOC_OBJ_L4CACHE:
    case HWLOC_OBJ_L5CACHE:
    case HWLOC_OBJ_L1ICACHE:
    case HWLOC_OBJ_L2ICACHE:
    case HWLOC_OBJ_L3ICACHE:
    case HWLOC_OBJ_CORE:
    case HWLOC_OBJ_PU:
    case HWLOC_OBJ_BRIDGE:
    case HWLOC_OBJ_PCI_DEVICE:
        return Disposition::keep;
    case HWLOC_OBJ_MEMCACHE:
        return Disposition::splice;
    default:
        return Disposition::drop;
    }
}

bool is_cache(hwloc_obj_type_t type) {
    return type >= HWLOC_OBJ_L1CACHE && type <= HWLOC_OBJ_L3ICACHE;
}

bool has_bus_id(hwloc_obj_t obj) {
    return obj->type == HWLOC_OBJ_PCI_DEVICE ||
           (obj->type == HWLOC_OBJ_BRIDGE &&
            obj->attr->bridge.upstream_type == HWLOC_OBJ_BRIDGE_PCI);
}

const PciAttr& pci_attr(hwloc_obj_t obj) {
    return obj->type == HWLOC_OBJ_PCI_DEVICE ? obj->attr->pcidev : obj->attr->bridge.upstream.pci;
}

std::string bus_id(const PciAttr& p) {
    std::array<char, 32> buf{};
    std::snprintf(buf.data(), buf.size(), "%04x:%02x:%02x.%01x", static_cast<unsigned>(p.domain),
                  static_cast<unsigned>(p.bus), static_cast<unsigned>(p.dev),
                  static_cast<unsigned>(p.func));
    return buf.data();
}

// Subsystem IDs and the revision are not in the §2.1 allowlist but hwloc's parser needs all six
// fields, so they are written as zeros.
std::string pci_type(const PciAttr& p) {
    std::array<char, 48> buf{};
    std::snprintf(buf.data(), buf.size(), "%04x [%04x:%04x] [0000:0000] 00",
                  static_cast<unsigned>(p.class_id), static_cast<unsigned>(p.vendor_id),
                  static_cast<unsigned>(p.device_id));
    return buf.data();
}

// GB/s as hwloc reports it: transfer rate per lane after line coding, times lanes. Gen 1 and 2
// use 8b/10b, gen 3 onward 128b/130b. The operation order matches generate.py so synthetic
// fixtures and captures print the same digits.
std::optional<std::string> link_speed(const PcieMax& max) {
    if (max.gen < 1 || max.gen > 6 || max.width <= 0) {
        return std::nullopt;
    }
    double gbps = 0.0;
    if (max.gen <= 2) {
        const double rate = 2.5 * static_cast<double>(1 << (max.gen - 1));
        gbps = rate * 8 / 10 / 8 * max.width;
    } else {
        const double rate = 8.0 * static_cast<double>(1 << (max.gen - 3));
        gbps = rate * 128 / 130 / 8 * max.width;
    }
    std::array<char, 32> buf{};
    std::snprintf(buf.data(), buf.size(), "%.6f", gbps);
    return std::string(buf.data());
}

// hwloc_bitmap_asprintf allocates with malloc.
struct FreeDeleter {
    void operator()(char* p) const { std::free(p); }
};

std::string bitmap(hwloc_const_bitmap_t set) {
    char* raw = nullptr;
    if (hwloc_bitmap_asprintf(&raw, set) < 0 || raw == nullptr) {
        return "0x0";
    }
    const std::unique_ptr<char, FreeDeleter> owned(raw);
    return owned.get();
}

// XML 1.0 allows only tab, LF and CR below 0x20, even as character references; the rest are
// dropped.
std::string escape(std::string_view text) {
    std::string out;
    out.reserve(text.size());
    for (const char c : text) {
        switch (c) {
        case '&':
            out += "&amp;";
            break;
        case '<':
            out += "&lt;";
            break;
        case '>':
            out += "&gt;";
            break;
        case '"':
            out += "&quot;";
            break;
        case '\t':
            out += "&#9;";
            break;
        case '\n':
            out += "&#10;";
            break;
        case '\r':
            out += "&#13;";
            break;
        default:
            if (static_cast<unsigned char>(c) >= 0x20) {
                out += c;
            }
        }
    }
    return out;
}

using Attrs = std::vector<std::pair<std::string_view, std::string>>;

class Emitter {
  public:
    Emitter(hwloc_topology_t topo, const PcieMaxMap& pcie_max) : topo_(topo), pcie_max_(pcie_max) {}

    std::string run() {
        out_ = "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
               "<!DOCTYPE topology SYSTEM \"hwloc2.dtd\">\n"
               "<topology version=\"2.0\">\n";
        object(hwloc_get_root_obj(topo_), 1);
        out_ += "</topology>\n";
        return std::move(out_);
    }

    void report(Diagnostics& diag) const {
        for (const auto& [type, count] : dropped_objects_) {
            std::string line = "hwloc.xml: dropped ";
            line += std::to_string(count);
            line += " ";
            line += type;
            line += " object(s)";
            diag.add(line);
        }
        if (dropped_infos_ > 0) {
            std::string line = "hwloc.xml: dropped ";
            line += std::to_string(dropped_infos_);
            line += " info key(s) outside the allowlist";
            diag.add(line);
        }
    }

  private:
    void children(hwloc_obj_t obj, int depth) {
        for (hwloc_obj_t c = obj->memory_first_child; c != nullptr; c = c->next_sibling) {
            object(c, depth);
        }
        for (hwloc_obj_t c = obj->first_child; c != nullptr; c = c->next_sibling) {
            object(c, depth);
        }
        for (hwloc_obj_t c = obj->io_first_child; c != nullptr; c = c->next_sibling) {
            object(c, depth);
        }
        for (hwloc_obj_t c = obj->misc_first_child; c != nullptr; c = c->next_sibling) {
            object(c, depth);
        }
    }

    void object(hwloc_obj_t obj, int depth) {
        switch (disposition(obj->type)) {
        case Disposition::splice:
            ++dropped_objects_[hwloc_obj_type_string(obj->type)];
            children(obj, depth);
            return;
        case Disposition::drop:
            ++dropped_objects_[hwloc_obj_type_string(obj->type)];
            return;
        case Disposition::keep:
            break;
        }

        const std::string indent(static_cast<std::size_t>(depth) * 2, ' ');
        out_ += indent;
        out_ += "<object";
        for (const auto& [name, value] : attributes(obj)) {
            out_ += ' ';
            out_ += name;
            out_ += "=\"";
            out_ += escape(value);
            out_ += '"';
        }

        // Children are rendered first so an object without any closes as an empty element.
        std::string body;
        std::swap(body, out_);
        content(obj, depth + 1);
        std::swap(body, out_);
        if (body.empty()) {
            out_ += "/>\n";
            return;
        }
        out_ += ">\n";
        out_ += body;
        out_ += indent;
        out_ += "</object>\n";
    }

    // Attribute order follows hwloc's own export, which generate.py also follows.
    Attrs attributes(hwloc_obj_t obj) const {
        Attrs a;
        a.emplace_back("type", hwloc_obj_type_string(obj->type));
        if (obj->os_index != HWLOC_UNKNOWN_INDEX) {
            a.emplace_back("os_index", std::to_string(obj->os_index));
        }
        const bool root = obj->parent == nullptr;
        if (obj->cpuset != nullptr) {
            a.emplace_back("cpuset", bitmap(obj->cpuset));
            a.emplace_back("complete_cpuset", bitmap(obj->complete_cpuset));
            if (root) {
                a.emplace_back("allowed_cpuset", bitmap(hwloc_topology_get_allowed_cpuset(topo_)));
            }
        }
        if (obj->nodeset != nullptr) {
            a.emplace_back("nodeset", bitmap(obj->nodeset));
            a.emplace_back("complete_nodeset", bitmap(obj->complete_nodeset));
            if (root) {
                a.emplace_back("allowed_nodeset",
                               bitmap(hwloc_topology_get_allowed_nodeset(topo_)));
            }
        }
        a.emplace_back("gp_index", std::to_string(obj->gp_index));

        if (is_cache(obj->type)) {
            const auto& c = obj->attr->cache;
            a.emplace_back("cache_size", std::to_string(c.size));
            a.emplace_back("depth", std::to_string(c.depth));
            a.emplace_back("cache_linesize", std::to_string(c.linesize));
            a.emplace_back("cache_associativity", std::to_string(c.associativity));
            a.emplace_back("cache_type", std::to_string(static_cast<int>(c.type)));
        } else if (obj->type == HWLOC_OBJ_GROUP) {
            // Without dont_merge a re-import may merge the group into its parent.
            const auto& g = obj->attr->group;
            a.emplace_back("kind", std::to_string(g.kind));
            a.emplace_back("subkind", std::to_string(g.subkind));
            if (g.dont_merge != 0) {
                a.emplace_back("dont_merge", "1");
            }
        } else if (obj->type == HWLOC_OBJ_NUMANODE && obj->attr->numanode.local_memory != 0) {
            a.emplace_back("local_memory", std::to_string(obj->attr->numanode.local_memory));
        } else if (obj->type == HWLOC_OBJ_BRIDGE) {
            const auto& b = obj->attr->bridge;
            std::string bridge_type = std::to_string(static_cast<int>(b.upstream_type));
            bridge_type += '-';
            bridge_type += std::to_string(static_cast<int>(b.downstream_type));
            a.emplace_back("bridge_type", std::move(bridge_type));
            a.emplace_back("depth", std::to_string(b.depth));
            if (b.downstream_type == HWLOC_OBJ_BRIDGE_PCI) {
                std::array<char, 32> buf{};
                std::snprintf(buf.data(), buf.size(), "%04x:[%02x-%02x]",
                              static_cast<unsigned>(b.downstream.pci.domain),
                              static_cast<unsigned>(b.downstream.pci.secondary_bus),
                              static_cast<unsigned>(b.downstream.pci.subordinate_bus));
                a.emplace_back("bridge_pci", buf.data());
            }
        }

        if (has_bus_id(obj)) {
            const PciAttr& p = pci_attr(obj);
            a.emplace_back("pci_busid", bus_id(p));
            a.emplace_back("pci_type", pci_type(p));
            // RFC-0003 §2.1: the maximum from sysfs, never hwloc's current speed, which drops to
            // gen 1 on an idle GPU.
            if (const std::optional<std::string> speed = link_speed(max_of(obj))) {
                a.emplace_back("pci_link_speed", *speed);
            }
        }
        return a;
    }

    PcieMax max_of(hwloc_obj_t obj) const {
        const auto it = pcie_max_.find(bus_id(pci_attr(obj)));
        return it == pcie_max_.end() ? PcieMax{} : it->second;
    }

    void element(int depth, std::string_view tag, const Attrs& attrs) {
        out_.append(static_cast<std::size_t>(depth) * 2, ' ');
        out_ += '<';
        out_ += tag;
        for (const auto& [name, value] : attrs) {
            out_ += ' ';
            out_ += name;
            out_ += "=\"";
            out_ += escape(value);
            out_ += '"';
        }
        out_ += "/>\n";
    }

    void content(hwloc_obj_t obj, int depth) {
        if (obj->type == HWLOC_OBJ_NUMANODE) {
            const auto& n = obj->attr->numanode;
            for (unsigned i = 0; i < n.page_types_len; ++i) {
                element(depth, "page_type",
                        {{"size", std::to_string(n.page_types[i].size)},
                         {"count", std::to_string(n.page_types[i].count)}});
            }
        }
        for (unsigned i = 0; i < obj->infos_count; ++i) {
            const auto& info = obj->infos[i];
            const std::string_view name = info.name != nullptr ? info.name : "";
            if (name == "OstiaPCIeMaxGen" || name == "OstiaPCIeMaxWidth") {
                continue; // rewritten below from pcie_max
            }
            if (std::ranges::find(kInfoAllowlist, name) == kInfoAllowlist.end()) {
                ++dropped_infos_;
                continue;
            }
            element(depth, "info", {{"name", std::string(name)}, {"value", info.value}});
        }
        if (has_bus_id(obj)) {
            const PcieMax max = max_of(obj);
            if (max.gen != 0) {
                element(depth, "info",
                        {{"name", "OstiaPCIeMaxGen"}, {"value", std::to_string(max.gen)}});
            }
            if (max.width != 0) {
                element(depth, "info",
                        {{"name", "OstiaPCIeMaxWidth"}, {"value", std::to_string(max.width)}});
            }
        }
        children(obj, depth);
    }

    hwloc_topology_t topo_;
    const PcieMaxMap& pcie_max_;
    std::string out_;
    std::map<std::string, int> dropped_objects_;
    int dropped_infos_ = 0;
};

// RFC-0003 §2.1: disallowed PUs stay in the topology so replay is host-independent; the
// restriction is only reported, as counts.
void report_restriction(hwloc_topology_t topo, Diagnostics& diag) {
    hwloc_const_cpuset_t allowed_pus = hwloc_topology_get_allowed_cpuset(topo);
    int pus = 0;
    int allowed = 0;
    for (hwloc_obj_t pu = hwloc_get_next_obj_by_type(topo, HWLOC_OBJ_PU, nullptr); pu != nullptr;
         pu = hwloc_get_next_obj_by_type(topo, HWLOC_OBJ_PU, pu)) {
        ++pus;
        allowed += hwloc_bitmap_isset(allowed_pus, pu->os_index) != 0 ? 1 : 0;
    }
    if (allowed < pus) {
        std::string line = "cpuset restricted: ";
        line += std::to_string(allowed);
        line += " of ";
        line += std::to_string(pus);
        line += " PUs allowed";
        diag.add(line);
    }
    hwloc_const_nodeset_t allowed_nodes = hwloc_topology_get_allowed_nodeset(topo);
    int nodes = 0;
    int allowed_n = 0;
    for (hwloc_obj_t n = hwloc_get_next_obj_by_type(topo, HWLOC_OBJ_NUMANODE, nullptr);
         n != nullptr; n = hwloc_get_next_obj_by_type(topo, HWLOC_OBJ_NUMANODE, n)) {
        ++nodes;
        allowed_n += hwloc_bitmap_isset(allowed_nodes, n->os_index) != 0 ? 1 : 0;
    }
    if (allowed_n < nodes) {
        std::string line = "nodeset restricted: ";
        line += std::to_string(allowed_n);
        line += " of ";
        line += std::to_string(nodes);
        line += " NUMA nodes allowed";
        diag.add(line);
    }
}

struct TopologyDeleter {
    void operator()(hwloc_topology* topo) const { hwloc_topology_destroy(topo); }
};

std::optional<int> parse_int(const std::string& text) {
    int value = 0;
    const char* const last = text.data() + text.size();
    const auto [ptr, ec] = std::from_chars(text.data(), last, value);
    if (ec != std::errc{} || ptr != last) {
        return std::nullopt;
    }
    return value;
}

} // namespace

std::string emit_xml(hwloc_topology_t topo, const PcieMaxMap& pcie_max, Diagnostics& diag) {
    Emitter emitter(topo, pcie_max);
    std::string xml = emitter.run();
    emitter.report(diag);
    report_restriction(topo, diag);
    return xml;
}

void reimport_check(const std::string& xml) {
    hwloc_topology_t raw = nullptr;
    if (hwloc_topology_init(&raw) != 0) {
        throw TopologyError("xml", "hwloc.xml", "hwloc_topology_init failed");
    }
    const std::unique_ptr<hwloc_topology, TopologyDeleter> topo(raw);
    // The replay flags (RFC-0003 §2.1), so what passes here is what FixtureSource loads.
    hwloc_topology_set_flags(raw, HWLOC_TOPOLOGY_FLAG_INCLUDE_DISALLOWED);
    hwloc_topology_set_io_types_filter(raw, HWLOC_TYPE_FILTER_KEEP_IMPORTANT);
    // hwloc's buffer length counts the terminating NUL.
    const int size = static_cast<int>(xml.size() + 1);
    if (hwloc_topology_set_xmlbuffer(raw, xml.c_str(), size) != 0 ||
        hwloc_topology_load(raw) != 0) {
        throw TopologyError("xml", "hwloc.xml", "hwloc could not re-import the emitted topology");
    }
}

PcieMaxMap read_pcie_max(const SysfsReader& sysfs) {
    PcieMaxMap map;
    const std::string devices = "bus/pci/devices/";
    for (const std::string& dev : sysfs.list(devices)) {
        std::string base = devices;
        base += dev;
        const std::optional<std::string> speed = sysfs.read(base + "/max_link_speed");
        const std::optional<std::string> width = sysfs.read(base + "/max_link_width");
        const PcieMax max{.gen = speed ? parse_pcie_gen(*speed) : 0,
                          .width = width ? parse_int(*width).value_or(0) : 0};
        if (max.gen != 0 || max.width != 0) {
            map.emplace(dev, max);
        }
    }
    return map;
}

} // namespace ostia::fabric::topology::capture
