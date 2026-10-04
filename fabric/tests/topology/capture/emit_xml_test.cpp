#include <algorithm>
#include <array>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <gtest/gtest.h>
#include <hwloc.h>
#include <iterator>
#include <memory>
#include <nlohmann/json.hpp>
#include <string>
#include <unistd.h>
#include <vector>

#include "core/diagnostics.hpp"
#include "core/emit_xml.hpp"
#include "core/sysfs.hpp"
#include "topology/builder.hpp"
#include "topology/error.hpp"
#include "topology/fixture_source.hpp"
#include "topology/hwloc_facts.hpp"
#include "topology/model.hpp"

using ostia::fabric::topology::build;
using ostia::fabric::topology::bus_id_of;
using ostia::fabric::topology::FixtureSource;
using ostia::fabric::topology::PcieMax;
using ostia::fabric::topology::PcieMaxMap;
using ostia::fabric::topology::to_json;
using ostia::fabric::topology::TopologyError;
using ostia::fabric::topology::capture::Diagnostics;
using ostia::fabric::topology::capture::emit_xml;
using ostia::fabric::topology::capture::read_pcie_max;
using ostia::fabric::topology::capture::reimport_check;
using ostia::fabric::topology::capture::SysfsReader;

namespace {

namespace fs = std::filesystem;
using nlohmann::json;

struct TopologyDeleter {
    void operator()(hwloc_topology* topo) const { hwloc_topology_destroy(topo); }
};
using TopologyPtr = std::unique_ptr<hwloc_topology, TopologyDeleter>;

// The flags FixtureSource replays with, so emit sees what replay sees.
TopologyPtr load(const fs::path& xml) {
    hwloc_topology_t raw = nullptr;
    if (hwloc_topology_init(&raw) != 0) {
        return nullptr;
    }
    TopologyPtr topo(raw);
    hwloc_topology_set_flags(raw, HWLOC_TOPOLOGY_FLAG_INCLUDE_DISALLOWED);
    hwloc_topology_set_io_types_filter(raw, HWLOC_TYPE_FILTER_KEEP_IMPORTANT);
    if (hwloc_topology_set_xml(raw, xml.c_str()) != 0 || hwloc_topology_load(raw) != 0) {
        return nullptr;
    }
    return topo;
}

int info_int(hwloc_obj_t obj, const char* name) {
    const char* value = hwloc_obj_get_info_by_name(obj, name);
    return value != nullptr ? std::atoi(value) : 0;
}

// What a fixture recorded, as the map a live capture would read from sysfs.
PcieMaxMap pcie_max_from_infos(hwloc_topology_t topo) {
    PcieMaxMap map;
    auto add = [&map](hwloc_obj_t obj) {
        const PcieMax max{.gen = info_int(obj, "OstiaPCIeMaxGen"),
                          .width = info_int(obj, "OstiaPCIeMaxWidth")};
        if (max.gen != 0 || max.width != 0) {
            map[bus_id_of(obj).value()] = max;
        }
    };
    for (hwloc_obj_t p = hwloc_get_next_pcidev(topo, nullptr); p != nullptr;
         p = hwloc_get_next_pcidev(topo, p)) {
        add(p);
    }
    for (hwloc_obj_t b = hwloc_get_next_bridge(topo, nullptr); b != nullptr;
         b = hwloc_get_next_bridge(topo, b)) {
        if (b->attr->bridge.upstream_type == HWLOC_OBJ_BRIDGE_PCI) {
            add(b);
        }
    }
    return map;
}

std::string read_file(const fs::path& path) {
    std::ifstream in(path, std::ios::binary);
    return {std::istreambuf_iterator<char>(in), std::istreambuf_iterator<char>()};
}

std::vector<fs::path> fixture_dirs() {
    std::vector<fs::path> dirs;
    for (const fs::directory_entry& entry :
         fs::recursive_directory_iterator(fs::path(OSTIA_TOPO_FIXTURE_DIR))) {
        if (entry.is_regular_file() && entry.path().filename() == "hwloc.xml") {
            dirs.push_back(entry.path().parent_path());
        }
    }
    std::ranges::sort(dirs);
    return dirs;
}

// A pair node's golden is its slot in the pair's expected.json.
json expected_model(const fs::path& dir) {
    if (fs::exists(dir / "expected.json")) {
        return json::parse(read_file(dir / "expected.json"));
    }
    const fs::path pair = dir.parent_path();
    const json nodes = json::parse(read_file(pair / "pair.json"))["nodes"];
    const json expected = json::parse(read_file(pair / "expected.json"));
    for (std::size_t i = 0; i < nodes.size(); ++i) {
        if (nodes[i] == dir.filename().string()) {
            return expected["nodes"][i];
        }
    }
    return nullptr;
}

class TempDir : public testing::Test {
  protected:
    void SetUp() override {
        const testing::TestInfo* info = testing::UnitTest::GetInstance()->current_test_info();
        // Each discovered test runs in its own process, possibly in parallel.
        std::string name = "ostia-emit-";
        name += info->name();
        name += "-";
        name += std::to_string(getpid());
        dir_ = fs::path(testing::TempDir()) / name;
        fs::remove_all(dir_);
        fs::create_directories(dir_);
    }
    void TearDown() override { fs::remove_all(dir_); }

    // The emitted XML beside the fixture's JSON files, ready for FixtureSource.
    fs::path stage(const fs::path& fixture, const std::string& xml, const std::string& name) {
        const fs::path out = dir_ / name;
        fs::create_directories(out);
        std::ofstream(out / "hwloc.xml", std::ios::binary) << xml;
        for (const fs::directory_entry& entry : fs::directory_iterator(fixture)) {
            const fs::path& p = entry.path();
            if (p.extension() == ".json" && p.filename() != "expected.json") {
                fs::copy_file(p, out / p.filename());
            }
        }
        return out;
    }

    fs::path dir_;
};

fs::path input_xml() { return fs::path(OSTIA_TOPO_DATA_DIR) / "hwloc-input.xml"; }
fs::path fake_root() { return fs::path(OSTIA_TOPO_DATA_DIR) / "capture-root" / "sys"; }

} // namespace

TEST_F(TempDir, EveryFixtureRoundTripsToItsGolden) {
    const std::vector<fs::path> dirs = fixture_dirs();
    ASSERT_FALSE(dirs.empty());
    bool saw_pair_node = false;
    int n = 0;
    for (const fs::path& dir : dirs) {
        const std::string rel = fs::relative(dir, OSTIA_TOPO_FIXTURE_DIR).string();
        SCOPED_TRACE(rel);
        saw_pair_node = saw_pair_node || dir.filename().string().starts_with("node-");
        const TopologyPtr topo = load(dir / "hwloc.xml");
        ASSERT_NE(topo, nullptr);
        Diagnostics diag;
        const std::string xml = emit_xml(topo.get(), pcie_max_from_infos(topo.get()), diag);
        EXPECT_NO_THROW(reimport_check(xml));

        const fs::path out = stage(dir, xml, "fixture-" + std::to_string(n++));
        const json expected = expected_model(dir);
        ASSERT_FALSE(expected.is_null());
        EXPECT_EQ(to_json(build(FixtureSource(out).facts())), expected);

        // The emitter is a fixed point on its own output.
        const TopologyPtr again = load(out / "hwloc.xml");
        ASSERT_NE(again, nullptr);
        Diagnostics diag2;
        EXPECT_EQ(emit_xml(again.get(), pcie_max_from_infos(again.get()), diag2), xml);
    }
    EXPECT_TRUE(saw_pair_node);
}

TEST(EmitXml, ScrubsIdentifiersAndOsDevices) {
    const TopologyPtr topo = load(input_xml());
    ASSERT_NE(topo, nullptr);
    // The OS-device drop path must actually run. hwloc drops the planted Misc object on load,
    // so only its absence from the output is asserted.
    ASSERT_NE(hwloc_get_next_osdev(topo.get(), nullptr), nullptr);
    Diagnostics diag;
    const std::string xml = emit_xml(topo.get(), read_pcie_max(SysfsReader(fake_root())), diag);

    // Names and values are planted in hwloc-input.xml. Failures print the index, never the
    // value.
    const std::array<std::string, 25> forbidden{
        "HostName",
        "DMIProductUUID",
        "DMIProductName",
        "DMIBoardSerial",
        "OSName",
        "OSRelease",
        "OSVersion",
        "Architecture",
        "NVIDIAUUID",
        "NVIDIASerial",
        "NodeGUID",
        "Address",
        "PCISlot",
        "SerialNumber",
        "DeviceLocation",
        "enx02005e100001",
        "02:00:5e:10:00:01",
        "ip-192-0-2-17",
        "ec2a1b2c-0000-4000-8000-0123456789ab",
        "GPU-0a1b2c3d-0000-4000-8000-0123456789ab",
        "OSTIA-GPU-SERIAL-0001",
        "OSTIA-NVME-SERIAL-0001",
        "OSTIA-DIMM-SERIAL-0001",
        "OstiaFakeSlot-0001",
        "0200:5eff:fe20:0001",
    };
    for (std::size_t i = 0; i < forbidden.size(); ++i) {
        EXPECT_EQ(xml.find(forbidden[i]), std::string::npos) << "planted entry " << i;
    }
    EXPECT_EQ(xml.find("OSDev"), std::string::npos);
    EXPECT_EQ(xml.find("Misc"), std::string::npos);
    EXPECT_EQ(xml.find("MemoryModule"), std::string::npos);
    EXPECT_EQ(xml.find("subtype="), std::string::npos);
    EXPECT_EQ(xml.find("name=\"card0\""), std::string::npos);
    EXPECT_EQ(xml.find("name=\"mlx5_1\""), std::string::npos);
    // Subsystem IDs and the revision are not in the allowlist.
    EXPECT_EQ(xml.find("16ca"), std::string::npos);

    // Allowlisted facts survive, escaped where needed.
    EXPECT_NE(xml.find(R"(<info name="CPUVendor" value="OstiaFakeVendor"/>)"), std::string::npos);
    EXPECT_NE(xml.find(R"(value="Ostia &quot;Fake&quot; CPU &amp; Co")"), std::string::npos);
    EXPECT_NE(xml.find(R"(<info name="Backend" value="Linux"/>)"), std::string::npos);
    EXPECT_NE(xml.find(R"(<info name="PCIVendor" value="NVIDIA Corporation"/>)"),
              std::string::npos);
    EXPECT_NE(xml.find(R"(<page_type size="4096" count="16777216"/>)"), std::string::npos);
    EXPECT_NE(xml.find(R"(type="L1Cache")"), std::string::npos);

    // Link facts come from sysfs maxima, not hwloc's current link speed; a device sysfs has no
    // maxima for gets no keys and no speed.
    EXPECT_NE(xml.find(R"(pci_busid="0000:11:00.0" pci_type="0302 [10de:27b8] [0000:0000] 00" )"
                       R"(pci_link_speed="31.507692")"),
              std::string::npos);
    EXPECT_NE(xml.find(R"(pci_busid="0000:12:00.0" pci_type="0207 [15b3:1021] [0000:0000] 00" )"
                       R"(pci_link_speed="63.015385")"),
              std::string::npos);
    EXPECT_NE(xml.find(R"(pci_busid="0000:13:00.0" pci_type="0108 [144d:a80a] [0000:0000] 00"/>)"),
              std::string::npos);
    EXPECT_NE(xml.find(R"(<info name="OstiaPCIeMaxGen" value="5"/>)"), std::string::npos);

    EXPECT_NO_THROW(reimport_check(xml));
    const std::vector<std::string>& lines = diag.lines();
    EXPECT_NE(std::ranges::find_if(lines,
                                   [](const std::string& line) {
                                       return line.starts_with("hwloc.xml: dropped ") &&
                                              line.ends_with(" OSDev object(s)");
                                   }),
              lines.end());
}

TEST(EmitXml, DiagnosticsCarryNoPlantedValue) {
    const TopologyPtr topo = load(input_xml());
    ASSERT_NE(topo, nullptr);
    Diagnostics diag;
    (void)emit_xml(topo.get(), {}, diag);
    const std::string text = diag.render();
    EXPECT_EQ(text.find("enx"), std::string::npos);
    EXPECT_EQ(text.find("02:00:5e"), std::string::npos);
    EXPECT_EQ(text.find("card0"), std::string::npos);
}

TEST(EmitXml, IsByteStable) {
    const TopologyPtr topo = load(input_xml());
    ASSERT_NE(topo, nullptr);
    const PcieMaxMap map = read_pcie_max(SysfsReader(fake_root()));
    Diagnostics diag;
    const std::string first = emit_xml(topo.get(), map, diag);
    const std::string second = emit_xml(topo.get(), map, diag);
    EXPECT_EQ(first, second);
}

TEST_F(TempDir, DisallowedPusAreKept) {
    const fs::path fixture = fs::path(OSTIA_TOPO_FIXTURE_DIR) / "synthetic" / "disallowed-pu";
    const TopologyPtr topo = load(fixture / "hwloc.xml");
    ASSERT_NE(topo, nullptr);
    Diagnostics diag;
    const std::string xml = emit_xml(topo.get(), pcie_max_from_infos(topo.get()), diag);
    EXPECT_NE(xml.find(R"(allowed_cpuset="0x00000003")"), std::string::npos);
    const std::vector<std::string>& lines = diag.lines();
    EXPECT_NE(std::ranges::find(lines, "cpuset restricted: 2 of 4 PUs allowed"), lines.end());
    EXPECT_EQ(FixtureSource(stage(fixture, xml, "disallowed")).facts().pus, 4);
}

TEST(EmitXml, UnrestrictedCpusetHasNoDiagnostic) {
    const fs::path fixture = fs::path(OSTIA_TOPO_FIXTURE_DIR) / "synthetic" / "no-nic";
    const TopologyPtr topo = load(fixture / "hwloc.xml");
    ASSERT_NE(topo, nullptr);
    Diagnostics diag;
    (void)emit_xml(topo.get(), {}, diag);
    EXPECT_EQ(diag.render().find("restricted"), std::string::npos);
}

TEST(EmitXml, ReimportCheckRejectsBrokenXml) {
    const TopologyPtr topo = load(input_xml());
    ASSERT_NE(topo, nullptr);
    Diagnostics diag;
    const std::string xml = emit_xml(topo.get(), {}, diag);
    try {
        reimport_check(xml.substr(0, xml.size() / 2));
        FAIL() << "a truncated document was accepted";
    } catch (const TopologyError& e) {
        EXPECT_EQ(e.code, "xml");
        EXPECT_EQ(e.file, "hwloc.xml");
    }
}

TEST(ReadPcieMax, ReadsSysfsMaximaPerBusId) {
    const PcieMaxMap map = read_pcie_max(SysfsReader(fake_root()));
    ASSERT_EQ(map.size(), 3U);
    EXPECT_EQ(map.at("0000:11:00.0").gen, 4);
    EXPECT_EQ(map.at("0000:11:00.0").width, 16);
    EXPECT_EQ(map.at("0000:12:00.0").gen, 5);
    EXPECT_EQ(map.count("0000:13:00.0"), 0U);
}
