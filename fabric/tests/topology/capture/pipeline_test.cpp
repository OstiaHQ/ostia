#include <algorithm>
#include <array>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <ctime>
#include <filesystem>
#include <fstream>
#include <gtest/gtest.h>
#include <hwloc.h>
#include <iterator>
#include <memory>
#include <nlohmann/json.hpp>
#include <optional>
#include <set>
#include <string>
#include <string_view>
#include <system_error>
#include <unistd.h>
#include <utility>
#include <variant>
#include <vector>

#include "core/apis.hpp"
#include "core/cleanup.hpp"
#include "core/diagnostics.hpp"
#include "core/manifest.hpp"
#include "core/pipeline.hpp"
#include "core/sysfs.hpp"
#include "fakes.hpp"
#include "topology/builder.hpp"
#include "topology/fixture_source.hpp"
#include "topology/hwloc_facts.hpp"
#include "topology/identity.hpp"
#include "topology/model.hpp"
#include "topology/schema.hpp"

using ostia::fabric::topology::build;
using ostia::fabric::topology::bus_id_of;
using ostia::fabric::topology::canonical_dump;
using ostia::fabric::topology::FixtureSource;
using ostia::fabric::topology::topo1;
using ostia::fabric::topology::validate;
using ostia::fabric::topology::capture::capture;
using ostia::fabric::topology::capture::CaptureOutcome;
using ostia::fabric::topology::capture::CleanupScope;
using ostia::fabric::topology::capture::Diagnostics;
using ostia::fabric::topology::capture::error_message;
using ostia::fabric::topology::capture::file_hash;
using ostia::fabric::topology::capture::Inputs;
using ostia::fabric::topology::capture::live_facts;
using ostia::fabric::topology::capture::MetaFlags;
using ostia::fabric::topology::capture::NotSupported;
using ostia::fabric::topology::capture::NvLink;
using ostia::fabric::topology::capture::NvmlFacts;
using ostia::fabric::topology::capture::NvmlGpu;
using ostia::fabric::topology::capture::P2p;
using ostia::fabric::topology::capture::print_id;
using ostia::fabric::topology::capture::run_signal_cleanup;
using ostia::fabric::topology::capture::SysfsReader;
using ostia::fabric::topology::capture::track_for_cleanup;
using ostia::fabric::topology::capture::VerbsPort;
using ostia::fabric::topology::capture::write_failed_capture;
using ostia::fabric::topology::capture::fakes::FakeNvml;
using ostia::fabric::topology::capture::fakes::FakeVerbs;

namespace {

namespace fs = std::filesystem;
using nlohmann::json;

constexpr std::int64_t kGib = std::int64_t{1} << 30;

// Planted in the fake root and the fake NVML; failure messages print an index, never one of these.
constexpr std::array<std::string_view, 8> kPlanted{
    "02:00:5e:10:00:01",     "0200:5eff:fe20:0001", "2001:0db8:0000:0000:0200:5eff:fe20:0001",
    "i-0123456789abcdef0",   "ec2-serial-planted",  "GPU-0a1b2c3d-0000-4000-8000-0123456789ab",
    "OSTIA-GPU-SERIAL-0001", "OSTIA-BOARD-0001",
};

using ostia::fabric::topology::TopologyPtr;

// The flags the live capture and FixtureSource use.
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

std::string read_file(const fs::path& path) {
    std::ifstream in(path, std::ios::binary);
    return {std::istreambuf_iterator<char>(in), std::istreambuf_iterator<char>()};
}

json read_json(const fs::path& path) { return json::parse(read_file(path)); }

void write_file(const fs::path& path, const std::string& text) {
    fs::create_directories(path.parent_path());
    std::ofstream(path, std::ios::binary) << text;
}

std::set<std::string> names_in(const fs::path& dir) {
    std::set<std::string> names;
    for (const fs::directory_entry& entry : fs::directory_iterator(dir)) {
        names.insert(entry.path().filename().string());
    }
    return names;
}

std::vector<std::string> lines_of(const std::string& text) {
    std::vector<std::string> lines;
    std::size_t start = 0;
    while (start < text.size()) {
        const std::size_t end = text.find('\n', start);
        const std::size_t stop = end == std::string::npos ? text.size() : end;
        lines.push_back(text.substr(start, stop - start));
        start = stop + 1;
    }
    return lines;
}

std::size_t count_lines_with(const std::string& text, std::string_view needle) {
    const std::vector<std::string> lines = lines_of(text);
    return static_cast<std::size_t>(std::ranges::count_if(lines, [needle](const std::string& line) {
        return line.find(needle) != std::string::npos;
    }));
}

fs::path data_dir() { return OSTIA_TOPO_DATA_DIR; }
fs::path synthetic(std::string_view name) {
    return fs::path(OSTIA_TOPO_FIXTURE_DIR) / "synthetic" / name;
}

NvmlFacts l4_facts() {
    NvmlFacts facts;
    facts.gpus = {NvmlGpu{.bus_id = "0000:11:00.0",
                          .name = "OstiaFakeGPU-0001",
                          .cc_major = 8,
                          .cc_minor = 9,
                          .memory_bytes = 24 * kGib,
                          .nvlinks = NotSupported{},
                          .uuid = "GPU-0a1b2c3d-0000-4000-8000-0123456789ab",
                          .serial = "OSTIA-GPU-SERIAL-0001",
                          .board_id = "OSTIA-BOARD-0001"}};
    facts.driver = "550.54.15";
    facts.cuda = "12.4";
    return facts;
}

VerbsPort ib_port(const std::string& bus_id) {
    return VerbsPort{.device = "mlx5_1",
                     .bus_id = bus_id,
                     .state = "ACTIVE",
                     .link_layer = "InfiniBand",
                     .gpudirect = "nvidia_peermem",
                     .port = 1,
                     .active_speed = 128,
                     .active_width = 2};
}

// One machine's sources. Inputs point into it, so it stays where it was built.
struct Machine {
    TopologyPtr topo;
    SysfsReader sysfs;
    FakeNvml nvml;
    FakeVerbs verbs;

    [[nodiscard]] Inputs inputs() {
        Inputs in;
        in.topo = topo.get();
        in.nvml = &nvml;
        in.verbs = &verbs;
        in.sysfs = &sysfs;
        in.meta = MetaFlags{.provider = "test", .instance_type = "test-type", .node_index = 0};
        return in;
    }
};

// hwloc-input.xml over the committed fake root: one L4, an ethernet NIC and an InfiniBand NIC.
std::unique_ptr<Machine>
gpu_machine(FakeNvml nvml = FakeNvml(l4_facts()),
            FakeVerbs verbs = FakeVerbs(std::vector<VerbsPort>{ib_port("0000:12:00.0")})) {
    return std::make_unique<Machine>(
        Machine{.topo = load(data_dir() / "hwloc-input.xml"),
                .sysfs = SysfsReader(data_dir() / "capture-root" / "sys"),
                .nvml = std::move(nvml),
                .verbs = std::move(verbs)});
}

std::string hex(unsigned value, int digits) {
    std::array<char, 16> buf{};
    std::snprintf(buf.data(), buf.size(), "0x%0*x", digits, value);
    return buf.data();
}

std::string gen_text(int gen) {
    switch (gen) {
    case 3:
        return "8.0 GT/s PCIe";
    case 4:
        return "16.0 GT/s PCIe";
    case 5:
        return "32.0 GT/s PCIe";
    default:
        return "Unknown";
    }
}

// The sysfs a live machine shaped like the fixture would show: every PCI device and PCI bridge
// with its class and maximum link facts, and the NICs' drivers, NUMA nodes and port facts.
void write_fake_root(hwloc_topology_t topo, const json& nics, const fs::path& root) {
    const fs::path devices = root / "bus" / "pci" / "devices";
    const auto add = [&devices](hwloc_obj_t obj, unsigned cls, unsigned vendor, unsigned device) {
        const fs::path dir = devices / bus_id_of(obj).value();
        write_file(dir / "class", hex(cls << 8, 6));
        write_file(dir / "vendor", hex(vendor, 4));
        write_file(dir / "device", hex(device, 4));
        const char* gen = hwloc_obj_get_info_by_name(obj, "OstiaPCIeMaxGen");
        const char* width = hwloc_obj_get_info_by_name(obj, "OstiaPCIeMaxWidth");
        if (gen != nullptr) {
            write_file(dir / "max_link_speed", gen_text(std::atoi(gen)) + "\n");
        }
        if (width != nullptr) {
            write_file(dir / "max_link_width", std::string(width) + "\n");
        }
    };
    for (hwloc_obj_t p = hwloc_get_next_pcidev(topo, nullptr); p != nullptr;
         p = hwloc_get_next_pcidev(topo, p)) {
        add(p, p->attr->pcidev.class_id, p->attr->pcidev.vendor_id, p->attr->pcidev.device_id);
    }
    for (hwloc_obj_t b = hwloc_get_next_bridge(topo, nullptr); b != nullptr;
         b = hwloc_get_next_bridge(topo, b)) {
        if (b->attr->bridge.upstream_type == HWLOC_OBJ_BRIDGE_PCI) {
            const auto& up = b->attr->bridge.upstream.pci;
            add(b, up.class_id, up.vendor_id, up.device_id);
        }
    }
    int index = 0;
    for (const json& nic : nics["nics"]) {
        const fs::path dir = devices / nic["bus_id"].get<std::string>();
        fs::create_symlink(fs::path("../../drivers") / nic["driver"].get<std::string>(),
                           dir / "driver");
        write_file(dir / "numa_node", std::to_string(nic["numa_node"].get<int>()) + "\n");
        const json& speed = nic["port_speed_mbps"];
        if (speed.is_number_integer() && nic["link_layer"] == "ethernet") {
            write_file(dir / "net" / "eth0" / "speed", std::to_string(speed.get<int>()) + "\n");
            write_file(dir / "net" / "eth0" / "type", "1\n");
        } else if (speed.is_number_integer() && nic["link_layer"] == "infiniband") {
            const fs::path port =
                dir / "infiniband" / ("ib" + std::to_string(index)) / "ports" / "1";
            write_file(port / "rate",
                       std::to_string(speed.get<int>() / 1000) + " Gb/sec (4X NDR)\n");
            write_file(port / "link_layer", "InfiniBand\n");
        }
        ++index;
    }
}

NvmlFacts nvml_from(const json& doc) {
    NvmlFacts facts;
    int n = 0;
    for (const json& g : doc["gpus"]) {
        NvmlGpu gpu{.bus_id = g["bus_id"].get<std::string>(),
                    .name = g["name"].get<std::string>(),
                    .cc_major = g["cc_major"].get<int>(),
                    .cc_minor = g["cc_minor"].get<int>(),
                    .memory_bytes = g["memory_bytes"].get<std::int64_t>(),
                    .nvlinks = NotSupported{},
                    .uuid = "GPU-0a1b2c3d-0000-4000-8000-00000000000" + std::to_string(n),
                    .serial = "OSTIA-SYNTH-SERIAL-000" + std::to_string(n),
                    .board_id = "OSTIA-SYNTH-BOARD-000" + std::to_string(n)};
        ++n;
        if (g["nvlinks"].is_array()) {
            std::vector<NvLink> links;
            for (const json& l : g["nvlinks"]) {
                links.push_back(NvLink{
                    .link = l["link"].get<int>(),
                    .state = l["state"].get<std::string>(),
                    .version = l["version"].get<int>(),
                    .remote_type = l["remote_type"].get<std::string>(),
                    .remote_bus_id =
                        l.contains("remote_bus_id")
                            ? std::optional<std::string>(l["remote_bus_id"].get<std::string>())
                            : std::nullopt});
            }
            gpu.nvlinks = std::move(links);
        }
        facts.gpus.push_back(std::move(gpu));
    }
    for (const json& p : doc["p2p"]) {
        facts.p2p.push_back(P2p{.a = p["a"].get<std::string>(),
                                .b = p["b"].get<std::string>(),
                                .read = p["read"].get<std::string>(),
                                .write = p["write"].get<std::string>(),
                                .nvlink = p["nvlink"].get<std::string>(),
                                .atomics = p["atomics"].get<std::string>()});
    }
    facts.driver = "550.54.15";
    facts.cuda = "12.4";
    return facts;
}

std::optional<std::vector<VerbsPort>> ports_from(const json& nics) {
    if (nics["rdma_probe"] != "ok") {
        return std::nullopt;
    }
    std::vector<VerbsPort> ports;
    for (const json& r : nics["rdma"]) {
        const json& speed = r["active_speed"];
        const json& width = r["active_width"];
        ports.push_back(
            VerbsPort{.device = r["device"].get<std::string>(),
                      .bus_id = r["bus_id"].get<std::string>(),
                      .state = r["state"].get<std::string>(),
                      .link_layer = r["link_layer"].get<std::string>(),
                      .gpudirect = r["gpudirect"].get<std::string>(),
                      .port = r["port"].get<int>(),
                      .active_speed = speed.is_string()
                                          ? std::variant<int, std::string>(speed.get<std::string>())
                                          : std::variant<int, std::string>(speed.get<int>()),
                      .active_width = width.is_string()
                                          ? std::variant<int, std::string>(width.get<std::string>())
                                          : std::variant<int, std::string>(width.get<int>())});
    }
    return ports;
}

// A synthetic fixture as a live machine: its hwloc.xml, a generated sysfs root under root, and
// NVML and verbs fakes that report what its JSON files record.
std::unique_ptr<Machine> synthetic_machine(const fs::path& fixture, const fs::path& root) {
    TopologyPtr topo = load(fixture / "hwloc.xml");
    const json nics = read_json(fixture / "nics.json");
    write_fake_root(topo.get(), nics, root);
    FakeNvml nvml = fs::exists(fixture / "nvml.json")
                        ? FakeNvml(nvml_from(read_json(fixture / "nvml.json")))
                        : FakeNvml::failing("nvml_not_expected");
    return std::make_unique<Machine>(Machine{.topo = std::move(topo),
                                             .sysfs = SysfsReader(root),
                                             .nvml = std::move(nvml),
                                             .verbs = FakeVerbs(ports_from(nics))});
}

class Pipeline : public testing::Test {
  protected:
    void SetUp() override {
        const testing::TestInfo* info = testing::UnitTest::GetInstance()->current_test_info();
        // Each discovered test runs in its own process, possibly in parallel.
        std::string name = "ostia-pipeline-";
        name += info->name();
        name += '-';
        name += std::to_string(getpid());
        dir_ = fs::path(testing::TempDir()) / name;
        fs::remove_all(dir_);
        tmp_ = dir_ / "tmp";
        fs::create_directories(tmp_);
        out_ = dir_ / "out";
        // The scratch and --print-id directories go under $TMPDIR; pointing it here lets a test
        // see that nothing is left behind.
        if (const char* old = std::getenv("TMPDIR")) {
            old_tmpdir_ = old;
        }
        setenv("TMPDIR", tmp_.c_str(), 1);
    }
    void TearDown() override {
        if (old_tmpdir_) {
            setenv("TMPDIR", old_tmpdir_->c_str(), 1);
        } else {
            unsetenv("TMPDIR");
        }
        fs::remove_all(dir_);
    }

    [[nodiscard]] bool tmp_is_empty() const { return fs::is_empty(tmp_); }
    [[nodiscard]] json manifest() const { return read_json(out_ / "manifest.json"); }
    [[nodiscard]] std::string diagnostics() const { return read_file(out_ / "diagnostics.txt"); }

    // A failed capture keeps only the manifest and the diagnostics (RFC-0003 §4).
    void expect_failed_shape(const CaptureOutcome& outcome) const {
        const json m = manifest();
        EXPECT_TRUE(validate("manifest", m).empty());
        EXPECT_EQ(m["status"], "failed");
        EXPECT_EQ(m["files"], json::object());
        EXPECT_TRUE(m["topology_id"].is_null());
        EXPECT_FALSE(outcome.topology_id.has_value());
        EXPECT_EQ(names_in(out_), (std::set<std::string>{"diagnostics.txt", "manifest.json"}));
        EXPECT_TRUE(tmp_is_empty());
    }

    fs::path dir_, tmp_, out_;
    std::optional<std::string> old_tmpdir_;
};

} // namespace

TEST_F(Pipeline, CompleteGpuMachine) {
    const auto machine = gpu_machine();
    Diagnostics diag;
    const CaptureOutcome outcome = capture(machine->inputs(), out_, diag);
    ASSERT_EQ(outcome.exit_code, 0);

    const json m = manifest();
    EXPECT_TRUE(validate("manifest", m).empty());
    EXPECT_EQ(m["status"], "complete");
    EXPECT_EQ(m["sanitized"], true);
    EXPECT_EQ(m["leak_check"], "passed");
    EXPECT_EQ(m["missing"], json::array());
    EXPECT_EQ(m["errors"], json::array());
    const std::set<std::string> data{"hwloc.xml", "meta.json", "nics.json", "nvml.json"};
    std::set<std::string> listed;
    for (const auto& [name, hash] : m["files"].items()) {
        listed.insert(name);
        EXPECT_EQ(hash, file_hash(read_file(out_ / name))) << name;
    }
    EXPECT_EQ(listed, data);
    std::set<std::string> expected_names = data;
    expected_names.insert({"manifest.json", "diagnostics.txt"});
    EXPECT_EQ(names_in(out_), expected_names);

    const std::string id = topo1(build(FixtureSource(out_).facts()));
    EXPECT_EQ(m["topology_id"], id);
    EXPECT_EQ(outcome.topology_id, id);
    const json meta = read_json(out_ / "meta.json");
    EXPECT_EQ(meta["tool_version"], m["tool_version"]);
    EXPECT_EQ(meta["driver_version"], "550.54.15");
    EXPECT_EQ(meta["provider"], "test");
    EXPECT_EQ(read_json(out_ / "nics.json")["rdma"].size(), 1U);
    EXPECT_TRUE(tmp_is_empty());
}

TEST_F(Pipeline, TcpOnlyMachineIsComplete) {
    const auto machine = synthetic_machine(synthetic("pair-tcp") / "node-0", dir_ / "root");
    Diagnostics diag;
    const CaptureOutcome outcome = capture(machine->inputs(), out_, diag);
    ASSERT_EQ(outcome.exit_code, 0);
    EXPECT_EQ(manifest()["status"], "complete");
    const json nics = read_json(out_ / "nics.json");
    EXPECT_EQ(nics["rdma_probe"], "ok");
    EXPECT_EQ(nics["rdma"], json::array());
    ASSERT_EQ(nics["nics"].size(), 1U);
    EXPECT_EQ(nics["nics"][0]["port_speed_mbps"], 25000);
    EXPECT_EQ(nics["nics"][0]["link_layer"], "ethernet");
}

TEST_F(Pipeline, NoGpuMachineIsCompleteWithoutNvml) {
    const auto machine = synthetic_machine(synthetic("unknown-port"), dir_ / "root");
    Diagnostics diag;
    const CaptureOutcome outcome = capture(machine->inputs(), out_, diag);
    ASSERT_EQ(outcome.exit_code, 0);
    const json m = manifest();
    EXPECT_EQ(m["status"], "complete");
    EXPECT_EQ(m["missing"], json::array());
    EXPECT_FALSE(m["files"].contains("nvml.json"));
    EXPECT_FALSE(fs::exists(out_ / "nvml.json"));
    EXPECT_EQ(machine->nvml.calls(), 0);
    const json meta = read_json(out_ / "meta.json");
    EXPECT_EQ(meta["driver_version"], "unknown");
    EXPECT_EQ(meta["cuda_version"], "unknown");
    EXPECT_EQ(read_json(out_ / "nics.json")["rdma_probe"], "unavailable");
}

TEST_F(Pipeline, NvmlInitErrorOnAGpuMachineIsPartial) {
    const auto machine = gpu_machine(FakeNvml::failing("nvml_init"));
    Diagnostics diag;
    const CaptureOutcome outcome = capture(machine->inputs(), out_, diag);
    ASSERT_EQ(outcome.exit_code, 2);
    const json m = manifest();
    EXPECT_TRUE(validate("manifest", m).empty());
    EXPECT_EQ(m["status"], "partial");
    EXPECT_EQ(m["missing"], json::parse(R"([{"file": "nvml.json", "reason": "nvml_init"}])"));
    EXPECT_EQ(m["sanitized"], true);
    EXPECT_EQ(m["leak_check"], "passed");
    EXPECT_FALSE(fs::exists(out_ / "nvml.json"));
    const std::string id = topo1(build(FixtureSource(out_).facts()));
    EXPECT_EQ(m["topology_id"], id);
    EXPECT_EQ(outcome.topology_id, id);
    EXPECT_EQ(read_json(out_ / "meta.json")["driver_version"], "unknown");
}

TEST_F(Pipeline, SchemaFailureIsExit3WithoutData) {
    const auto machine = gpu_machine();
    Inputs in = machine->inputs();
    in.hooks.before_validate = [](const fs::path& staged) {
        json nics = read_json(staged / "nics.json");
        nics["ostia_unexpected"] = 1;
        write_file(staged / "nics.json", nics.dump());
    };
    Diagnostics diag;
    const CaptureOutcome outcome = capture(in, out_, diag);
    ASSERT_EQ(outcome.exit_code, 3);
    expect_failed_shape(outcome);
    const json m = manifest();
    EXPECT_EQ(m["sanitized"], false);
    EXPECT_EQ(m["leak_check"], "not_run");
    ASSERT_EQ(m["errors"].size(), 1U);
    EXPECT_EQ(m["errors"][0]["code"], "schema");
    EXPECT_NE(diagnostics().find("schema: nics.json violation at /ostia_unexpected"),
              std::string::npos);
}

TEST_F(Pipeline, LeakIsExit3AndDiagnosticsNameNoValue) {
    const auto machine = gpu_machine();
    Inputs in = machine->inputs();
    // The fake GPU's name is written to nvml.json, so declaring it an identifier plants a leak.
    in.extra = {"OstiaFakeGPU-0001"};
    Diagnostics diag;
    const CaptureOutcome outcome = capture(in, out_, diag);
    ASSERT_EQ(outcome.exit_code, 3);
    expect_failed_shape(outcome);
    const json m = manifest();
    EXPECT_EQ(m["sanitized"], true);
    EXPECT_EQ(m["leak_check"], "failed");
    ASSERT_EQ(m["errors"].size(), 1U);
    EXPECT_EQ(m["errors"][0]["code"], "leak");
    const std::string text = diagnostics();
    EXPECT_EQ(count_lines_with(text, " /gpus/0/name: extra"), 1U);
    EXPECT_EQ(count_lines_with(text, "leak: nvml.json:"), 1U);
    EXPECT_EQ(text.find("OstiaFakeGPU"), std::string::npos) << "diagnostics carry the value";
    EXPECT_EQ(m.dump().find("OstiaFakeGPU"), std::string::npos) << "manifest carries the value";
}

TEST_F(Pipeline, ReimportFailureIsExit1) {
    const auto machine = gpu_machine();
    Inputs in = machine->inputs();
    in.hooks.before_reimport = [](std::string& xml) { xml.resize(xml.size() / 2); };
    Diagnostics diag;
    const CaptureOutcome outcome = capture(in, out_, diag);
    ASSERT_EQ(outcome.exit_code, 1);
    expect_failed_shape(outcome);
    const json m = manifest();
    ASSERT_EQ(m["errors"].size(), 1U);
    EXPECT_EQ(m["errors"][0]["code"], "xml_reimport");
    EXPECT_EQ(m["errors"][0]["message"], error_message("xml_reimport"));
    EXPECT_EQ(m["sanitized"], false);
    EXPECT_EQ(m["leak_check"], "not_run");
}

TEST_F(Pipeline, UnknownLinksSchemaIsExit1) {
    const auto machine = gpu_machine();
    Inputs in = machine->inputs();
    const fs::path links = dir_ / "links.json";
    write_file(links, R"({"schema": 2, "links": []})");
    in.links = links;
    Diagnostics diag;
    const CaptureOutcome outcome = capture(in, out_, diag);
    ASSERT_EQ(outcome.exit_code, 1);
    expect_failed_shape(outcome);
    const json m = manifest();
    ASSERT_EQ(m["errors"].size(), 1U);
    EXPECT_EQ(m["errors"][0]["code"], "links_schema");
    EXPECT_NE(m["errors"][0]["message"].get<std::string>().find("supported: 1"), std::string::npos);
}

TEST_F(Pipeline, ValidLinksAreWrittenAndListed) {
    const auto machine = gpu_machine();
    Inputs in = machine->inputs();
    const fs::path links = dir_ / "links.json";
    write_file(links, R"({"schema": 1, "links": []})");
    in.links = links;
    Diagnostics diag;
    ASSERT_EQ(capture(in, out_, diag).exit_code, 0);
    EXPECT_TRUE(manifest()["files"].contains("links.json"));
    EXPECT_EQ(read_json(out_ / "links.json"), json::parse(R"({"schema": 1, "links": []})"));
}

TEST_F(Pipeline, ANonEmptyOutWithoutAManifestIsRefused) {
    write_file(out_ / "keep.txt", "not a capture\n");
    const auto machine = gpu_machine();
    Diagnostics diag;
    testing::internal::CaptureStderr();
    const CaptureOutcome outcome = capture(machine->inputs(), out_, diag);
    const std::string err = testing::internal::GetCapturedStderr();
    EXPECT_EQ(outcome.exit_code, 1);
    EXPECT_EQ(names_in(out_), std::set<std::string>{"keep.txt"});
    EXPECT_NE(err.find("fix: remove <out> or choose an empty directory"), std::string::npos);
    EXPECT_EQ(machine->nvml.calls(), 0);
}

TEST_F(Pipeline, APreviousCaptureIsRemovedFirst) {
    {
        const auto machine = gpu_machine();
        Diagnostics diag;
        ASSERT_EQ(capture(machine->inputs(), out_, diag).exit_code, 0);
    }
    ASSERT_TRUE(fs::exists(out_ / "nvml.json"));
    // Not part of the capture, so it is left alone.
    write_file(out_ / "notes.txt", "kept\n");
    // A reserved name the old manifest does not list is removed all the same.
    write_file(out_ / "links.json", "{}\n");
    const auto machine = gpu_machine(FakeNvml::failing("nvml_init"));
    Diagnostics diag;
    ASSERT_EQ(capture(machine->inputs(), out_, diag).exit_code, 2);
    EXPECT_FALSE(fs::exists(out_ / "nvml.json"));
    EXPECT_EQ(names_in(out_),
              (std::set<std::string>{"diagnostics.txt", "hwloc.xml", "manifest.json", "meta.json",
                                     "nics.json", "notes.txt"}));
    EXPECT_EQ(manifest()["status"], "partial");
}

TEST_F(Pipeline, AThrowingSourceIsExit1AndRemovesTheScratchDirectory) {
    const auto machine = gpu_machine(FakeNvml::throwing());
    Diagnostics diag;
    const CaptureOutcome outcome = capture(machine->inputs(), out_, diag);
    ASSERT_EQ(outcome.exit_code, 1);
    expect_failed_shape(outcome);
    EXPECT_EQ(manifest()["errors"][0]["code"], "internal");
}

TEST_F(Pipeline, PrintIdLeavesNothingAndMatchesCapture) {
    const auto machine = gpu_machine();
    Diagnostics diag;
    const CaptureOutcome printed = print_id(machine->inputs(), diag);
    ASSERT_EQ(printed.exit_code, 0);
    ASSERT_TRUE(printed.topology_id.has_value());
    EXPECT_TRUE(tmp_is_empty());
    Diagnostics again;
    EXPECT_EQ(capture(machine->inputs(), out_, again).topology_id, printed.topology_id);
}

TEST_F(Pipeline, PrintIdOfAFailedCaptureLeavesNothing) {
    const auto machine = gpu_machine();
    Inputs in = machine->inputs();
    in.extra = {"OstiaFakeGPU-0001"};
    Diagnostics diag;
    const CaptureOutcome printed = print_id(in, diag);
    EXPECT_EQ(printed.exit_code, 3);
    EXPECT_FALSE(printed.topology_id.has_value());
    EXPECT_TRUE(tmp_is_empty());
}

TEST_F(Pipeline, TwoCapturesDifferOnlyInTheCaptureTime) {
    const auto machine = gpu_machine();
    Diagnostics first;
    Diagnostics second;
    ASSERT_EQ(capture(machine->inputs(), out_ / "a", first).exit_code, 0);
    ASSERT_EQ(capture(machine->inputs(), out_ / "b", second).exit_code, 0);
    for (const char* name : {"hwloc.xml", "nvml.json", "nics.json"}) {
        EXPECT_EQ(read_file(out_ / "a" / name), read_file(out_ / "b" / name)) << name;
    }
    json meta_a = read_json(out_ / "a" / "meta.json");
    json meta_b = read_json(out_ / "b" / "meta.json");
    meta_a.erase("captured_at");
    meta_b.erase("captured_at");
    EXPECT_EQ(meta_a, meta_b);
    json manifest_a = read_json(out_ / "a" / "manifest.json");
    json manifest_b = read_json(out_ / "b" / "manifest.json");
    manifest_a["files"].erase("meta.json");
    manifest_b["files"].erase("meta.json");
    EXPECT_EQ(manifest_a, manifest_b);
}

// The CPU twin of the GPU live_vs_replay test.
TEST_F(Pipeline, LiveFactsBuildTheReplayedModel) {
    const auto machine = synthetic_machine(synthetic("nvswitch-8nic-2numa"), dir_ / "root");
    const Inputs in = machine->inputs();
    Diagnostics diag;
    const CaptureOutcome outcome = capture(in, out_, diag);
    ASSERT_EQ(outcome.exit_code, 0);
    const auto live = build(live_facts(in));
    const auto replayed = build(FixtureSource(out_).facts());
    EXPECT_EQ(canonical_dump(live), canonical_dump(replayed));
    EXPECT_EQ(outcome.topology_id, topo1(live));
}

TEST_F(Pipeline, RdmaInSysfsWithoutVerbsDevicesWarnsOnce) {
    const auto machine = gpu_machine(FakeNvml(l4_facts()), FakeVerbs(std::vector<VerbsPort>{}));
    Diagnostics diag;
    testing::internal::CaptureStderr();
    const CaptureOutcome outcome = capture(machine->inputs(), out_, diag);
    const std::string err = testing::internal::GetCapturedStderr();
    ASSERT_EQ(outcome.exit_code, 0);
    EXPECT_EQ(count_lines_with(diag.render(), "but ibverbs listed none"), 1U);
    EXPECT_EQ(count_lines_with(err, "warning: "), 1U);
    EXPECT_EQ(count_lines_with(err, "ibverbs listed none"), 1U);
}

TEST_F(Pipeline, AVerbsPortOutsideHwlocIsSkipped) {
    const auto machine = gpu_machine(
        FakeNvml(l4_facts()),
        FakeVerbs(std::vector<VerbsPort>{ib_port("0000:12:00.0"), ib_port("0000:99:00.0")}));
    Diagnostics diag;
    const CaptureOutcome outcome = capture(machine->inputs(), out_, diag);
    ASSERT_EQ(outcome.exit_code, 0);
    EXPECT_EQ(count_lines_with(diag.render(), "verbs: skipped 1 port(s)"), 1U);
    const json nics = read_json(out_ / "nics.json");
    ASSERT_EQ(nics["rdma"].size(), 1U);
    EXPECT_EQ(nics["rdma"][0]["bus_id"], "0000:12:00.0");
}

// A VMD domain above 0xffff has no place in nics.json's bus ID form, so the NIC or port is
// dropped and counted rather than failing the capture on the schema.
TEST_F(Pipeline, BusIdsNicsJsonCannotHoldAreSkipped) {
    const fs::path root = dir_ / "root";
    fs::copy(data_dir() / "capture-root" / "sys", root,
             fs::copy_options::recursive | fs::copy_options::copy_symlinks);
    write_file(root / "bus" / "pci" / "devices" / "10000:00:01.0" / "class", "0x020000\n");
    auto machine = gpu_machine(
        FakeNvml(l4_facts()),
        FakeVerbs(std::vector<VerbsPort>{ib_port("0000:12:00.0"), ib_port("10000:12:00.0")}));
    machine->sysfs = SysfsReader(root);
    Diagnostics diag;
    const CaptureOutcome outcome = capture(machine->inputs(), out_, diag);
    ASSERT_EQ(outcome.exit_code, 0);
    const std::string text = diag.render();
    EXPECT_EQ(count_lines_with(text, "nics: skipped 1 NIC(s) whose bus ID nics.json cannot hold"),
              1U);
    EXPECT_EQ(count_lines_with(text, "verbs: skipped 1 port(s) whose bus ID nics.json cannot hold"),
              1U);
    const json nics = read_json(out_ / "nics.json");
    EXPECT_EQ(nics["nics"].size(), 2U);
    EXPECT_EQ(nics["rdma"].size(), 1U);
}

TEST_F(Pipeline, AnUnusableTmpdirIsInternalNotWriteFailed) {
    if (::geteuid() == 0) {
        GTEST_SKIP() << "root writes into a mode 0500 directory";
    }
    const auto machine = gpu_machine();
    fs::permissions(tmp_, fs::perms::owner_read | fs::perms::owner_exec);
    Diagnostics diag;
    const CaptureOutcome outcome = capture(machine->inputs(), out_, diag);
    fs::permissions(tmp_, fs::perms::owner_all);
    ASSERT_EQ(outcome.exit_code, 1);
    expect_failed_shape(outcome);
    const json m = manifest();
    ASSERT_EQ(m["errors"].size(), 1U);
    EXPECT_EQ(m["errors"][0]["code"], "internal");
    EXPECT_EQ(count_lines_with(diagnostics(), "no scratch directory"), 1U);
}

// The capture's own tracking ends when it returns; print_id keeps every name it could have
// written tracked until its directory is gone, so a signal in between leaves nothing.
TEST_F(Pipeline, ALateSignalInPrintIdLeavesNothing) {
    const auto machine = gpu_machine();
    Inputs in = machine->inputs();
    bool ran = false;
    in.hooks.after_print_id_capture = [&ran](const fs::path& dir) {
        ran = true;
        EXPECT_TRUE(fs::exists(dir / "hwloc.xml"));
        run_signal_cleanup();
        EXPECT_FALSE(fs::exists(dir));
    };
    Diagnostics diag;
    ASSERT_EQ(print_id(in, diag).exit_code, 0);
    EXPECT_TRUE(ran);
    EXPECT_TRUE(tmp_is_empty());
}

// RFC-0003 Performance: the largest expected machine. A whole-pipeline budget, so wider than the
// 1 s replay bound.
TEST_F(Pipeline, PrintIdOfTheLargestShapeIsFast) {
    const auto machine = synthetic_machine(synthetic("nvswitch-8nic-2numa"), dir_ / "root");
#ifdef OSTIA_TOPO_SANITIZED
    constexpr double kBudgetSeconds = 15.0;
#else
    constexpr double kBudgetSeconds = 5.0;
#endif
    Diagnostics diag;
    const std::clock_t start = std::clock();
    const CaptureOutcome outcome = print_id(machine->inputs(), diag);
    const double seconds = static_cast<double>(std::clock() - start) / CLOCKS_PER_SEC;
    ASSERT_EQ(outcome.exit_code, 0);
    EXPECT_LT(seconds, kBudgetSeconds);
    EXPECT_TRUE(tmp_is_empty());
}

TEST_F(Pipeline, StderrSummaryNamesTheRecordAndNoValue) {
    const auto machine = gpu_machine();
    Diagnostics diag;
    testing::internal::CaptureStderr();
    ASSERT_EQ(capture(machine->inputs(), out_, diag).exit_code, 0);
    const std::string err = testing::internal::GetCapturedStderr();
    EXPECT_NE(err.find(R"(provider "test", instance type "test-type")"), std::string::npos);
    EXPECT_NE(err.find("leak check: passed"), std::string::npos);
    EXPECT_NE(err.find("status: complete, exit 0"), std::string::npos);
    const std::string text = diagnostics();
    for (std::size_t i = 0; i < kPlanted.size(); ++i) {
        EXPECT_EQ(err.find(kPlanted[i]), std::string::npos) << "stderr carries planted value " << i;
        EXPECT_EQ(text.find(kPlanted[i]), std::string::npos)
            << "diagnostics carry planted value " << i;
    }
}

TEST_F(Pipeline, DiagnosticsTimeEachSource) {
    const auto machine = gpu_machine();
    Diagnostics diag;
    ASSERT_EQ(capture(machine->inputs(), out_, diag).exit_code, 0);
    const std::string text = diagnostics();
    for (const char* prefix :
         {"nvml: 1 GPU(s) in ", "verbs: 1 port(s) in ", "nics: 2 NIC(s) in ", "emit: hwloc.xml in ",
          "pcie: ", "leak search: ", "replay: topo1 in "}) {
        EXPECT_EQ(count_lines_with(text, prefix), 1U) << prefix;
    }
}

TEST_F(Pipeline, SignalCleanupRemovesTrackedPathsNewestFirst) {
    const fs::path scratch = dir_ / "scratch";
    {
        const CleanupScope scope;
        fs::create_directories(scratch);
        ASSERT_TRUE(track_for_cleanup(scratch, true));
        ASSERT_TRUE(track_for_cleanup(scratch / "staged.json", false));
        write_file(scratch / "staged.json", "{}");
        run_signal_cleanup();
        EXPECT_FALSE(fs::exists(scratch));
    }
    // The scope dropped its paths, so a later cleanup leaves a recreated directory alone.
    fs::create_directories(scratch);
    run_signal_cleanup();
    EXPECT_TRUE(fs::exists(scratch));
}

TEST_F(Pipeline, AFailedManifestWriteBecomesTheFailedForm) {
    const auto machine = gpu_machine();
    Inputs in = machine->inputs();
    int calls = 0;
    in.hooks.before_manifest = [&calls](const fs::path& /*out*/) {
        ++calls;
        throw std::system_error(std::make_error_code(std::errc::no_space_on_device), "test");
    };
    Diagnostics diag;
    testing::internal::CaptureStderr();
    const CaptureOutcome outcome = capture(in, out_, diag);
    const std::string err = testing::internal::GetCapturedStderr();
    ASSERT_EQ(outcome.exit_code, 1);
    EXPECT_EQ(calls, 1);
    expect_failed_shape(outcome);
    const json m = manifest();
    ASSERT_EQ(m["errors"].size(), 1U);
    EXPECT_EQ(m["errors"][0]["code"], "write_failed");
    EXPECT_NE(err.find("status: failed, exit 1"), std::string::npos);
    EXPECT_NE(diagnostics().find("capture: exit 1"), std::string::npos);
}

TEST_F(Pipeline, WriteFailedCaptureCreatesOut) {
    Diagnostics diag;
    const CaptureOutcome outcome = write_failed_capture(out_, "hwloc_load", diag);
    EXPECT_EQ(outcome.exit_code, 1);
    expect_failed_shape(outcome);
    EXPECT_EQ(manifest()["errors"][0]["code"], "hwloc_load");
    EXPECT_EQ(manifest()["leak_check"], "not_run");
}

TEST_F(Pipeline, WriteFailedCaptureClearsAPreviousCapture) {
    {
        const auto machine = gpu_machine();
        Diagnostics diag;
        ASSERT_EQ(capture(machine->inputs(), out_, diag).exit_code, 0);
    }
    Diagnostics diag;
    const CaptureOutcome outcome = write_failed_capture(out_, "internal", diag);
    EXPECT_EQ(outcome.exit_code, 1);
    expect_failed_shape(outcome);
    EXPECT_EQ(manifest()["errors"][0]["code"], "internal");
}

TEST_F(Pipeline, WriteFailedCaptureRefusesADirectoryHoldingNoCapture) {
    write_file(out_ / "keep.txt", "not a capture\n");
    Diagnostics diag;
    testing::internal::CaptureStderr();
    const CaptureOutcome outcome = write_failed_capture(out_, "hwloc_load", diag);
    const std::string err = testing::internal::GetCapturedStderr();
    EXPECT_EQ(outcome.exit_code, 1);
    EXPECT_EQ(names_in(out_), std::set<std::string>{"keep.txt"});
    EXPECT_NE(err.find("fix: remove <out> or choose an empty directory"), std::string::npos);
}

TEST_F(Pipeline, AReleasedScopeKeepsItsPathsFromSignalCleanup) {
    const fs::path kept = dir_ / "kept";
    const CleanupScope scope;
    fs::create_directories(kept);
    ASSERT_TRUE(track_for_cleanup(kept, true));
    scope.release();
    run_signal_cleanup();
    EXPECT_TRUE(fs::exists(kept));
}
