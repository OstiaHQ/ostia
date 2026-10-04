#include <filesystem>
#include <fstream>
#include <gtest/gtest.h>
#include <string>
#include <utility>
#include <variant>
#include <vector>

#include "core/apis.hpp"
#include "core/diagnostics.hpp"
#include "core/nics.hpp"
#include "core/sysfs.hpp"

using ostia::fabric::topology::capture::Diagnostics;
using ostia::fabric::topology::capture::NicFacts;
using ostia::fabric::topology::capture::parse_pcie_gen;
using ostia::fabric::topology::capture::scan_nics;
using ostia::fabric::topology::capture::SysfsReader;
using ostia::fabric::topology::capture::VerbsPort;

namespace {

namespace fs = std::filesystem;

fs::path fake_root() { return fs::path(OSTIA_TOPO_DATA_DIR) / "capture-root" / "sys"; }

bool has_line(const Diagnostics& diag, const std::string& line) {
    for (const std::string& have : diag.lines()) {
        if (have == line) {
            return true;
        }
    }
    return false;
}

// The committed tree is never mutated: tests that remove files work on a copy.
class CopiedRoot : public testing::Test {
  protected:
    void SetUp() override {
        copy_ = fs::path(testing::TempDir()) / "ostia-capture-root-copy";
        fs::remove_all(copy_);
        fs::create_directories(copy_);
        fs::copy(fake_root(), copy_, fs::copy_options::recursive | fs::copy_options::copy_symlinks);
    }
    void TearDown() override { fs::remove_all(copy_); }

    fs::path copy_;
};

} // namespace

TEST(Nics, FindsTheEthernetAndInfinibandNicsAndSkipsTheVf) {
    Diagnostics diag;
    const std::vector<NicFacts> nics = scan_nics(SysfsReader(fake_root()), {}, diag);
    ASSERT_EQ(nics.size(), 2U);
    EXPECT_EQ(nics[0].bus_id, "0000:11:01.0");
    EXPECT_EQ(nics[1].bus_id, "0000:12:00.0");
    EXPECT_TRUE(has_line(diag, "skipped 1 SR-IOV virtual function(s)"));
}

TEST(Nics, EthernetNicTakesSpeedAndLayerFromNet) {
    Diagnostics diag;
    const std::vector<NicFacts> nics = scan_nics(SysfsReader(fake_root()), {}, diag);
    ASSERT_EQ(nics.size(), 2U);
    EXPECT_EQ(nics[0].driver, "mlx5_core");
    EXPECT_EQ(nics[0].link_layer, "ethernet");
    EXPECT_EQ(std::get<int>(nics[0].port_speed_mbps), 100000);
    EXPECT_EQ(nics[0].max_gen, 4);
    EXPECT_EQ(nics[0].max_width, 16);
    EXPECT_EQ(nics[0].numa, 0);
}

TEST(Nics, InfinibandNicTakesSpeedAndLayerFromTheIbPort) {
    Diagnostics diag;
    const std::vector<NicFacts> nics = scan_nics(SysfsReader(fake_root()), {}, diag);
    ASSERT_EQ(nics.size(), 2U);
    EXPECT_EQ(nics[1].link_layer, "infiniband");
    EXPECT_EQ(std::get<int>(nics[1].port_speed_mbps), 400000);
    EXPECT_EQ(nics[1].max_gen, 5);
}

TEST_F(CopiedRoot, MissingInfinibandWithoutVerbsLeavesPortFactsUnknown) {
    fs::remove_all(copy_ / "bus/pci/devices/0000:12:00.0/infiniband");
    Diagnostics diag;
    const std::vector<NicFacts> nics = scan_nics(SysfsReader(copy_), {}, diag);
    ASSERT_EQ(nics.size(), 2U);
    EXPECT_EQ(nics[1].link_layer, "unknown");
    EXPECT_EQ(std::get<std::string>(nics[1].port_speed_mbps), "unknown");
    EXPECT_TRUE(has_line(
        diag, "nic 0000:12:00.0: port facts unknown (host network namespace not visible)"));
}

TEST_F(CopiedRoot, MissingInfinibandFallsBackToTheVerbsLinkLayer) {
    fs::remove_all(copy_ / "bus/pci/devices/0000:12:00.0/infiniband");
    VerbsPort port{"mlx5_1", "0000:12:00.0", "ACTIVE", "InfiniBand", "yes", 1, 0, 0};
    Diagnostics diag;
    const std::vector<NicFacts> nics = scan_nics(SysfsReader(copy_), {port}, diag);
    ASSERT_EQ(nics.size(), 2U);
    EXPECT_EQ(nics[1].link_layer, "infiniband");
    EXPECT_EQ(std::get<std::string>(nics[1].port_speed_mbps), "unknown");
}

TEST_F(CopiedRoot, DownLinkSpeedIsUnknown) {
    {
        std::ofstream out(copy_ / "bus/pci/devices/0000:11:01.0/net/ens5/speed");
        out << "-1\n";
    }
    Diagnostics diag;
    const std::vector<NicFacts> nics = scan_nics(SysfsReader(copy_), {}, diag);
    ASSERT_EQ(nics.size(), 2U);
    EXPECT_EQ(std::get<std::string>(nics[0].port_speed_mbps), "unknown");
}

TEST(Nics, ParsesEveryPcieGeneration) {
    const std::vector<std::pair<std::string, int>> table{
        {"2.5 GT/s PCIe", 1},  {"5.0 GT/s PCIe", 2},  {"8.0 GT/s PCIe", 3}, {"16.0 GT/s PCIe", 4},
        {"32.0 GT/s PCIe", 5}, {"64.0 GT/s PCIe", 6}, {"2.5 GT/s", 1},      {"5.0 GT/s", 2},
        {"8.0 GT/s", 3},       {"16.0 GT/s", 4},      {"32.0 GT/s", 5},     {"64.0 GT/s", 6},
    };
    for (const auto& [text, gen] : table) {
        EXPECT_EQ(parse_pcie_gen(text), gen) << "entry " << gen;
    }
}

TEST(Nics, UnrecognisedPcieSpeedIsZero) {
    EXPECT_EQ(parse_pcie_gen("Unknown"), 0);
    EXPECT_EQ(parse_pcie_gen(""), 0);
    EXPECT_EQ(parse_pcie_gen("16.0"), 0);
    EXPECT_EQ(parse_pcie_gen("160.0 GT/s PCIe"), 0);
    EXPECT_EQ(parse_pcie_gen("16.0 GT/s PCIe extra"), 0);
}

TEST_F(CopiedRoot, UnrecognisedMaxLinkSpeedAddsOneDiagnosticsLine) {
    {
        std::ofstream out(copy_ / "bus/pci/devices/0000:11:01.0/max_link_speed");
        out << "Unknown\n";
    }
    Diagnostics diag;
    const std::vector<NicFacts> nics = scan_nics(SysfsReader(copy_), {}, diag);
    ASSERT_EQ(nics.size(), 2U);
    EXPECT_EQ(nics[0].max_gen, 0);
    EXPECT_TRUE(has_line(diag, "nic 0000:11:01.0: unrecognised max_link_speed"));
}
