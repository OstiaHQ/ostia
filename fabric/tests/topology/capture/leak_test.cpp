#include <cctype>
#include <cstddef>
#include <cstdint>
#include <ctime>
#include <filesystem>
#include <fstream>
#include <gtest/gtest.h>
#include <string>
#include <string_view>
#include <unistd.h>
#include <utility>
#include <vector>

#include "core/apis.hpp"
#include "core/diagnostics.hpp"
#include "core/leak.hpp"
#include "core/sysfs.hpp"

using ostia::fabric::topology::capture::add_host_names;
using ostia::fabric::topology::capture::collect_raw;
using ostia::fabric::topology::capture::Diagnostics;
using ostia::fabric::topology::capture::expand_derived;
using ostia::fabric::topology::capture::Finding;
using ostia::fabric::topology::capture::IdKind;
using ostia::fabric::topology::capture::NvmlFacts;
using ostia::fabric::topology::capture::NvmlGpu;
using ostia::fabric::topology::capture::RawEntry;
using ostia::fabric::topology::capture::RawSet;
using ostia::fabric::topology::capture::read_extra_identifiers;
using ostia::fabric::topology::capture::search;
using ostia::fabric::topology::capture::SysfsReader;
using ostia::fabric::topology::capture::to_string;
using ostia::fabric::topology::capture::vpd_identifiers;

namespace {

namespace fs = std::filesystem;

fs::path fake_root() { return fs::path(OSTIA_TOPO_DATA_DIR) / "capture-root" / "sys"; }

fs::path synthetic_dir() { return fs::path(OSTIA_TOPO_FIXTURE_DIR) / "synthetic"; }

std::string lowered(std::string_view text) {
    std::string out;
    for (const char c : text) {
        out += static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    }
    return out;
}

// Every expectation compares these values-free strings, so a failure never prints an
// identifier, and each one is checked to hold none of the raw set's needles.
std::vector<std::string> described(const RawSet& raw, const std::vector<Finding>& findings) {
    std::vector<std::string> out;
    for (const Finding& finding : findings) {
        std::string text = to_string(finding);
        const std::string low = lowered(text);
        for (const RawEntry& entry : raw.entries()) {
            EXPECT_EQ(low.find(entry.needle), std::string::npos) << "a finding carries a value";
        }
        out.push_back(std::move(text));
    }
    return out;
}

std::size_t count_of(const RawSet& raw, IdKind kind) {
    const auto counts = raw.counts();
    const auto it = counts.find(kind);
    return it == counts.end() ? 0 : it->second;
}

// PCI VPD resources (PCI Local Bus 3.0 §I): a large tag with a 16-bit little-endian length.
std::string large_resource(std::uint8_t tag, const std::string& body) {
    constexpr unsigned kByte = 0xff;
    std::string out;
    out += static_cast<char>(tag);
    out += static_cast<char>(body.size() & kByte);
    out += static_cast<char>((body.size() >> 8U) & kByte);
    out += body;
    return out;
}

std::string vpd_field(std::string_view keyword, std::string_view value) {
    std::string out(keyword);
    out += static_cast<char>(value.size());
    out += value;
    return out;
}

constexpr std::uint8_t kIdString = 0x82;
constexpr std::uint8_t kVpdR = 0x90;
constexpr char kEndTag = 0x78;

// Each discovered test runs in its own process, possibly in parallel, so directories are
// per test and per process, and the committed tree is never written.
class Leak : public testing::Test {
  protected:
    void SetUp() override {
        const testing::TestInfo* info = testing::UnitTest::GetInstance()->current_test_info();
        std::string name = "ostia-leak-";
        name += info->name();
        name += "-";
        name += std::to_string(getpid());
        dir_ = fs::path(testing::TempDir()) / name;
        fs::remove_all(dir_);
        fs::create_directories(dir_);
    }
    void TearDown() override { fs::remove_all(dir_); }

    fs::path write(const std::string& name, const std::string& content) {
        const fs::path path = dir_ / name;
        std::ofstream(path, std::ios::binary) << content;
        return path;
    }

    // A copy of the fake sysfs root that a test may add files to.
    fs::path copied_root() {
        const fs::path copy = dir_ / "sys";
        fs::create_directories(copy);
        fs::copy(fake_root(), copy, fs::copy_options::recursive | fs::copy_options::copy_symlinks);
        return copy;
    }

    fs::path dir_;
};

} // namespace

TEST_F(Leak, MacIsFoundAsItsEui64LinkLocalGidInThreeSpellings) {
    RawSet raw;
    raw.add(IdKind::mac, "02:00:5e:10:00:01");
    expand_derived(raw);
    // The fourth line is the EUI-64 without the universal/local flip, which is not this MAC's.
    const fs::path out = write("out.txt", "gid fe80:0000:0000:0000:0000:5eff:fe10:0001\n"
                                          "GID FE80000000000000 00005EFFFE100001\n"
                                          "gid fe80::5eff:fe10:1\n"
                                          "gid fe80::200:5eff:fe10:1\n");
    const std::vector<std::string> expected{"out.txt:1: mac", "out.txt:2: mac", "out.txt:3: mac"};
    EXPECT_EQ(described(raw, search(raw, {out})), expected);
}

TEST_F(Leak, Ipv4IsFoundAsAMappedGidAndAnEc2Hostname) {
    RawSet raw;
    raw.add(IdKind::ipv4, "192.0.2.17");
    expand_derived(raw);
    const fs::path out = write("out.txt", "gid ::ffff:192.0.2.17\n"
                                          "gid 0000:0000:0000:0000:0000:ffff:c000:0211\n"
                                          "gid ::ffff:c000:211\n"
                                          "host ip-192-0-2-17 up\n");
    const std::vector<std::string> expected{"out.txt:1: ipv4", "out.txt:2: ipv4", "out.txt:3: ipv4",
                                            "out.txt:4: ipv4"};
    EXPECT_EQ(described(raw, search(raw, {out})), expected);
}

TEST_F(Leak, AMappedGidIsFoundAsItsIpv4) {
    RawSet raw;
    raw.add(IdKind::gid, "0000:0000:0000:0000:0000:ffff:c000:0211");
    expand_derived(raw);
    const fs::path out = write("out.txt", "addr 192.0.2.17\n"
                                          "host ip-192-0-2-17\n"
                                          "addr 192.0.2.170\n");
    const std::vector<std::string> expected{"out.txt:1: gid", "out.txt:2: gid"};
    EXPECT_EQ(described(raw, search(raw, {out})), expected);
}

TEST_F(Leak, IpoibAddressesAndTheirGidsAreFoundBothWays) {
    RawSet raw;
    raw.add(IdKind::gid, "fe80:0000:0000:0000:0200:5eff:fe20:0001");
    raw.add(IdKind::ipoib, "80:00:00:48:fe:80:00:00:00:00:00:00:02:00:5e:ff:fe:21:00:02");
    expand_derived(raw);
    const fs::path out =
        write("out.txt", "00:00:10:49:fe:80:00:00:00:00:00:00:02:00:5e:ff:fe:20:00:01\n"
                         "fe80::200:5eff:fe21:2\n");
    const std::vector<std::string> expected{"out.txt:1: gid", "out.txt:2: ipoib"};
    EXPECT_EQ(described(raw, search(raw, {out})), expected);
}

TEST_F(Leak, GuidMatchesWithAndWithoutColonsInEitherCase) {
    RawSet raw;
    raw.add(IdKind::guid, "0200:5eff:fe20:0001");
    const fs::path out = write("out.txt", "02005EFFFE200001\n"
                                          "02:00:5e:ff:fe:20:00:01\n"
                                          "0200:5EFF:FE20:0001\n");
    const std::vector<std::string> expected{"out.txt:1: guid", "out.txt:2: guid",
                                            "out.txt:3: guid"};
    EXPECT_EQ(described(raw, search(raw, {out})), expected);
}

TEST_F(Leak, DmiInstanceIdsAreFoundWholeAndEmbedded) {
    Diagnostics diag;
    RawSet raw = collect_raw(SysfsReader(fake_root()), nullptr, {}, diag, false);
    raw.add(IdKind::dmi, "aws/i-0a1b2c3d4e5f60718");
    expand_derived(raw);
    const fs::path out = write("out.txt", "tag i-0123456789abcdef0\n"
                                          "instance i-0a1b2c3d4e5f60718\n");
    const std::vector<std::string> expected{"out.txt:1: instance-id", "out.txt:2: instance-id"};
    EXPECT_EQ(described(raw, search(raw, {out})), expected);
}

TEST_F(Leak, RawSetTakesIdentifyingDmiFieldsOnlyAndSkipsZeroGids) {
    Diagnostics diag;
    const RawSet raw = collect_raw(SysfsReader(fake_root()), nullptr, {}, diag, false);
    // node_guid and sys_image_guid are equal; gids/1 is all zeros.
    EXPECT_EQ(count_of(raw, IdKind::mac), 1U);
    EXPECT_EQ(count_of(raw, IdKind::guid), 1U);
    EXPECT_EQ(count_of(raw, IdKind::gid), 1U);
    EXPECT_EQ(count_of(raw, IdKind::instance_id), 1U);
    EXPECT_EQ(count_of(raw, IdKind::dmi), 1U);
    const fs::path out = write("out.txt", "model g6.4xlarge\n"
                                          "vendor Amazon EC2\n"
                                          "serial ec2-serial-planted\n"
                                          "gid 0000:0000:0000:0000:0000:0000:0000:0000\n");
    const std::vector<std::string> expected{"out.txt:3: dmi"};
    EXPECT_EQ(described(raw, search(raw, {out})), expected);
}

TEST_F(Leak, EachRawSetSourceRecordsItsWallTime) {
    Diagnostics diag;
    const RawSet raw = collect_raw(SysfsReader(fake_root()), nullptr, {}, diag, false);
    for (const std::string_view source : {"sysfs-net", "infiniband", "vpd", "dmi", "extra"}) {
        std::string prefix = "leak raw set: ";
        prefix += source;
        prefix += ' ';
        bool seen = false;
        for (const std::string& line : diag.lines()) {
            seen = seen || (line.starts_with(prefix) && line.ends_with(" ms"));
        }
        EXPECT_TRUE(seen) << source;
    }
    EXPECT_FALSE(raw.entries().empty());
}

TEST_F(Leak, VpdSerialAndVendorFieldsOfNicsAreCollected) {
    const fs::path root = copied_root();
    std::string read_only = vpd_field("PN", "MCX623106AN-CDAT");
    read_only += vpd_field("SN", "MT2231X01234");
    read_only += vpd_field("V0", "OstiaVendorV0x");
    read_only += vpd_field("RV", std::string(1, '\0'));
    std::string vpd = large_resource(kIdString, "ConnectX-6 Dx");
    vpd += large_resource(kVpdR, read_only);
    vpd += kEndTag;
    vpd += large_resource(kVpdR, vpd_field("SN", "MT9999AFTEREND"));
    std::ofstream(root / "bus/pci/devices/0000:11:01.0/vpd", std::ios::binary) << vpd;
    // Not a network controller, so its VPD is not read.
    std::ofstream(root / "bus/pci/devices/0000:13:00.0/vpd", std::ios::binary)
        << large_resource(kVpdR, vpd_field("SN", "NVME-SERIAL-77"));

    Diagnostics diag;
    const RawSet raw = collect_raw(SysfsReader(root), nullptr, {}, diag, false);
    const fs::path out = write("out.txt", "MT2231X01234\n"
                                          "OstiaVendorV0x\n"
                                          "MCX623106AN-CDAT\n"
                                          "MT9999AFTEREND\n"
                                          "NVME-SERIAL-77\n");
    const std::vector<std::string> expected{"out.txt:1: serial", "out.txt:2: serial"};
    EXPECT_EQ(described(raw, search(raw, {out})), expected);
}

TEST_F(Leak, DmiPlaceholdersAreNotCollected) {
    const fs::path root = copied_root();
    std::ofstream(root / "class/dmi/id/product_serial") << "To Be Filled By O.E.M.\n";
    Diagnostics diag;
    const RawSet raw = collect_raw(SysfsReader(root), nullptr, {}, diag, false);
    EXPECT_EQ(count_of(raw, IdKind::dmi), 0U);
    EXPECT_EQ(count_of(raw, IdKind::instance_id), 1U);
}

TEST_F(Leak, VpdIdentifiersStopAtTheEndTag) {
    std::string read_only = vpd_field("PN", "MCX623106AN-CDAT");
    read_only += vpd_field("SN", "MT2231X01234");
    read_only += vpd_field("VA", "OstiaVendorVA");
    read_only += vpd_field("Vx", "lower-case-keyword");
    std::string vpd = large_resource(kIdString, "ConnectX-6 Dx");
    vpd += large_resource(kVpdR, read_only);
    vpd += kEndTag;
    vpd += large_resource(kVpdR, vpd_field("SN", "MT9999AFTEREND"));
    const std::vector<std::string> ids = vpd_identifiers(vpd);
    ASSERT_EQ(ids.size(), 2U);
    EXPECT_TRUE(ids[0] == "MT2231X01234");
    EXPECT_TRUE(ids[1] == "OstiaVendorVA");
    // Cut inside the PN field: a length running past the end is clipped, not read out of bounds.
    constexpr std::size_t kInsidePn = 29;
    EXPECT_TRUE(vpd_identifiers(vpd.substr(0, kInsidePn)).empty());
}

TEST_F(Leak, VpdBeyondFourKibIsNotRead) {
    const fs::path root = copied_root();
    constexpr std::size_t kPastCap = 4100;
    std::string vpd = large_resource(kIdString, std::string(kPastCap, 'x'));
    vpd += large_resource(kVpdR, vpd_field("SN", "MT2231X01234"));
    vpd += kEndTag;
    std::ofstream(root / "bus/pci/devices/0000:11:01.0/vpd", std::ios::binary) << vpd;

    Diagnostics diag;
    const RawSet raw = collect_raw(SysfsReader(root), nullptr, {}, diag, false);
    EXPECT_EQ(count_of(raw, IdKind::serial), 0U);
}

TEST_F(Leak, ShortValuesAreSkippedAndCounted) {
    RawSet raw;
    raw.add(IdKind::extra, "abcde");
    raw.add(IdKind::mac, "0a:0b:0");
    EXPECT_EQ(raw.skipped_short(), 2U);
    EXPECT_TRUE(raw.entries().empty());
    const fs::path out = write("out.txt", "abcde 0a0b0\n");
    EXPECT_TRUE(search(raw, {out}).empty());
}

TEST_F(Leak, LoopbackAndUnspecifiedAddressesAreDropped) {
    RawSet raw;
    for (const char* ip : {"127.0.0.1", "0.0.0.0"}) {
        raw.add(IdKind::ipv4, ip);
    }
    for (const char* ip : {"::1", "::", "::ffff:127.0.0.1"}) {
        raw.add(IdKind::ipv6, ip);
    }
    raw.add(IdKind::mac, "ff:ff:ff:ff:ff:ff");
    EXPECT_TRUE(raw.entries().empty());
}

TEST_F(Leak, Ipv4MatchesWholeTokensOnly) {
    RawSet raw;
    raw.add(IdKind::ipv4, "10.0.0.5");
    expand_derived(raw);
    const fs::path out = write("out.txt", "addr 10.0.0.50\n"
                                          "addr 110.0.0.5\n"
                                          "addr=10.0.0.5,\n");
    const std::vector<std::string> expected{"out.txt:3: ipv4"};
    EXPECT_EQ(described(raw, search(raw, {out})), expected);
}

TEST_F(Leak, ExtraIdentifiersAndNamesMatchAsTokens) {
    RawSet raw;
    raw.add(IdKind::extra, "OstiaFakeGPU-0001");
    raw.add(IdKind::hostname, "gke-test-pool-1a2b3c4d");
    const fs::path out = write("out.json", "{\"model\": \"OstiaFakeGPU-0001\", "
                                           "\"other\": \"xOstiaFakeGPU-00012\", "
                                           "\"name\": \"gke-test-pool-1a2b3c4d\"}\n");
    const std::vector<std::string> expected{"out.json:1 /model: extra",
                                            "out.json:1 /name: hostname"};
    EXPECT_EQ(described(raw, search(raw, {out})), expected);
}

TEST_F(Leak, FqdnComesFromTheDomainnameFileWithoutALookup) {
    const fs::path domain = write("domainname", "example.internal\n");
    const fs::path none = write("none", "(none)\n");
    const fs::path empty = write("empty", "\n");
    for (const fs::path& file : {none, empty, dir_ / "absent"}) {
        RawSet raw;
        add_host_names(raw, "node-a1b2c3", file);
        EXPECT_EQ(count_of(raw, IdKind::hostname), 1U) << file.filename();
    }
    RawSet raw;
    add_host_names(raw, "node-a1b2c3", domain);
    EXPECT_EQ(count_of(raw, IdKind::hostname), 2U);
    const fs::path out = write("out.txt", "host node-a1b2c3.example.internal\n");
    const std::vector<std::string> expected{"out.txt:1: hostname"};
    EXPECT_EQ(described(raw, search(raw, {out})), expected);

    // A dotted hostname is already an FQDN; its first label is added too.
    RawSet dotted;
    add_host_names(dotted, "node-a1b2c3.corp.internal", dir_ / "absent");
    EXPECT_EQ(count_of(dotted, IdKind::hostname), 2U);
}

TEST_F(Leak, ExtraIdentifiersFileIgnoresCommentsAndBlankLines) {
    const fs::path file = write("ids.txt", "# planted identifiers\n"
                                           "\n"
                                           "  OstiaFakeGPU-0001  \n"
                                           "   # indented comment\n"
                                           "second-ident\n");
    const std::vector<std::string> ids = read_extra_identifiers(file);
    ASSERT_EQ(ids.size(), 2U);
    EXPECT_TRUE(ids[0] == "OstiaFakeGPU-0001");
    EXPECT_TRUE(ids[1] == "second-ident");
    EXPECT_TRUE(read_extra_identifiers(dir_ / "absent.txt").empty());
}

TEST_F(Leak, JsonFindingsCarryAJsonPointer) {
    RawSet raw;
    raw.add(IdKind::extra, "mlx5_planted0");
    const fs::path out = write("nics.json", "{\n"
                                            "  \"nics\": [\n"
                                            "    {\n"
                                            "      \"bus_id\": \"0000:12:00.0\"\n"
                                            "    }\n"
                                            "  ],\n"
                                            "  \"rdma\": [\n"
                                            "    {\n"
                                            "      \"bus_id\": \"0000:12:00.0\",\n"
                                            "      \"device\": \"mlx5_planted0\",\n"
                                            "      \"port\": 1\n"
                                            "    }\n"
                                            "  ]\n"
                                            "}\n");
    const std::vector<std::string> expected{"nics.json:10 /rdma/0/device: extra"};
    EXPECT_EQ(described(raw, search(raw, {out})), expected);
}

TEST_F(Leak, XmlFindingsCarryAnElementPathAndAttribute) {
    RawSet raw;
    raw.add(IdKind::extra, "planted-slot-0077");
    const fs::path out =
        write("hwloc.xml", "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
                           "<!DOCTYPE topology SYSTEM \"hwloc2.dtd\">\n"
                           "<topology version=\"2.0\">\n"
                           "  <object type=\"Machine\" os_index=\"0\">\n"
                           "    <object type=\"PCIDev\">\n"
                           "      <info name=\"PCISlot\" value=\"planted-slot-0077\"/>\n"
                           "    </object>\n"
                           "    <object type=\"Misc\">planted-slot-0077</object>\n"
                           "  </object>\n"
                           "</topology>\n");
    const std::vector<std::string> expected{
        "hwloc.xml:6 /topology/object/object/info/@value: extra",
        "hwloc.xml:8 /topology/object/object: extra"};
    EXPECT_EQ(described(raw, search(raw, {out})), expected);
}

TEST_F(Leak, ALocatorThatWouldCarryAValueIsWithheld) {
    RawSet raw;
    raw.add(IdKind::hostname, "gke-test-pool-1a2b3c4d");
    const fs::path out = write("pair.json", "{\"gke-test-pool-1a2b3c4d\": {\"zone\": \"a\"}}\n");
    const std::vector<std::string> expected{"pair.json:1 (withheld): hostname"};
    EXPECT_EQ(described(raw, search(raw, {out})), expected);
}

TEST_F(Leak, UnreadableFilesAreReported) {
    RawSet raw;
    raw.add(IdKind::extra, "OstiaFakeGPU-0001");
    const std::vector<std::string> expected{"missing.json:0 unreadable"};
    EXPECT_EQ(described(raw, search(raw, {dir_ / "missing.json"})), expected);
}

// RFC-0003 §3: legitimate output must not trip the check, so a realistic raw set finds nothing
// in any synthetic fixture. A failure here is fixed in the patterns, never in the fixtures.
TEST_F(Leak, NothingIsFoundInTheSyntheticFixtures) {
    NvmlFacts nvml;
    nvml.gpus.push_back(NvmlGpu{.bus_id = "0000:11:00.0",
                                .name = "NVIDIA L4",
                                .cc_major = 8,
                                .cc_minor = 9,
                                .memory_bytes = 0,
                                .nvlinks = {},
                                .uuid = "GPU-6b9f5664-1234-5678-9abc-def012345678",
                                .serial = "1652520012345",
                                .board_id = "6400"});
    const std::vector<std::string> extra{"OstiaFakeGPU-0001", "gke-test-pool-1a2b3c4d",
                                         "node-a1b2c3"};
    Diagnostics diag;
    RawSet raw = collect_raw(SysfsReader(fake_root()), &nvml, extra, diag, false);
    raw.add(IdKind::ipv4, "10.0.1.23");
    raw.add(IdKind::ipv4, "192.0.2.17");
    raw.add(IdKind::ipv6, "2001:db8::17");
    raw.add(IdKind::mac, "0c:42:a1:5e:6f:70");
    raw.add(IdKind::guid, "0c42:a103:00a1:b2c3");
    raw.add(IdKind::machine_id, "4c4c4544003510108035b4c04f4e3432");
    raw.add(IdKind::hostname, "ip-10-0-1-23.ec2.internal");
    expand_derived(raw);

    std::vector<fs::path> files;
    for (const fs::directory_entry& entry : fs::recursive_directory_iterator(synthetic_dir())) {
        const std::string ext = entry.path().extension().string();
        if (entry.is_regular_file() && (ext == ".json" || ext == ".xml")) {
            files.push_back(entry.path());
        }
    }
    ASSERT_GT(files.size(), 30U);
    EXPECT_EQ(described(raw, search(raw, files)), std::vector<std::string>{});
}

TEST_F(Leak, TenThousandGidsSearchTheLargestFixtureQuickly) {
    RawSet raw;
    // An inline LCG (Knuth's MMIX constants): <random> is not allowed in compiled code.
    std::uint64_t state = 0x05714aU;
    const auto group = [&state]() {
        state = state * 6364136223846793005ULL + 1442695040888963407ULL;
        constexpr std::string_view kDigits = "0123456789abcdef";
        std::string out;
        for (unsigned shift = 48; shift < 64; shift += 4) {
            out += kDigits[(state >> shift) & 0x0fU];
        }
        return out;
    };
    constexpr int kGids = 10000;
    for (int i = 0; i < kGids; ++i) {
        std::string gid = "fe80:0000:0000:0000";
        for (int g = 0; g < 4; ++g) {
            gid += ':';
            gid += group();
        }
        raw.add(IdKind::gid, std::move(gid));
    }
    expand_derived(raw);
    std::vector<fs::path> files;
    for (const fs::directory_entry& entry :
         fs::directory_iterator(synthetic_dir() / "nvswitch-hidden")) {
        files.push_back(entry.path());
    }

    const std::clock_t start = std::clock();
    const std::vector<Finding> findings = search(raw, files);
    const double seconds = static_cast<double>(std::clock() - start) / CLOCKS_PER_SEC;
#ifdef OSTIA_TOPO_SANITIZED
    constexpr double kBudget = 5.0;
#else
    constexpr double kBudget = 1.0;
#endif
    EXPECT_LT(seconds, kBudget);
    EXPECT_TRUE(findings.empty());
}
