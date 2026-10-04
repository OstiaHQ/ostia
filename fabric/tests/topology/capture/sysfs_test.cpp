#include <filesystem>
#include <gtest/gtest.h>
#include <string>

#include "core/sysfs.hpp"

using ostia::fabric::topology::capture::SysfsReader;

namespace {

SysfsReader fake_root() {
    return SysfsReader(std::filesystem::path(OSTIA_TOPO_DATA_DIR) / "capture-root" / "sys");
}

} // namespace

TEST(Sysfs, ReadTrimsTheTrailingNewline) {
    const SysfsReader sysfs = fake_root();
    EXPECT_EQ(sysfs.read("class/dmi/id/product_name"), "g6.4xlarge");
    EXPECT_EQ(sysfs.read("bus/pci/devices/0000:11:00.0/max_link_speed"), "16.0 GT/s PCIe");
}

TEST(Sysfs, ReadBytesKeepsTheNewline) {
    const SysfsReader sysfs = fake_root();
    EXPECT_EQ(sysfs.read_bytes("class/dmi/id/product_name"), "g6.4xlarge\n");
}

TEST(Sysfs, AbsentFilesAreNullopt) {
    const SysfsReader sysfs = fake_root();
    EXPECT_FALSE(sysfs.read("class/dmi/id/no_such_file").has_value());
    EXPECT_FALSE(sysfs.read_bytes("no/such/dir/file").has_value());
}

TEST(Sysfs, ListIsSortedAndEmptyWhenAbsent) {
    const SysfsReader sysfs = fake_root();
    const std::vector<std::string> expected{"0000:11:00.0", "0000:11:01.0", "0000:12:00.0",
                                            "0000:12:00.2", "0000:13:00.0"};
    EXPECT_EQ(sysfs.list("bus/pci/devices"), expected);
    EXPECT_TRUE(sysfs.list("bus/pci/nothing").empty());
}

TEST(Sysfs, LinkNameIsTheTargetBasename) {
    const SysfsReader sysfs = fake_root();
    EXPECT_EQ(sysfs.link_name("bus/pci/devices/0000:11:00.0/driver"), "nvidia");
    EXPECT_EQ(sysfs.link_name("bus/pci/devices/0000:12:00.2/physfn"), "0000:12:00.0");
    EXPECT_EQ(sysfs.link_name("class/net/ens5"), "ens5");
}

TEST(Sysfs, LinkNameIsNulloptForNonLinksAndAbsentPaths) {
    const SysfsReader sysfs = fake_root();
    EXPECT_FALSE(sysfs.link_name("class/dmi/id/product_name").has_value());
    EXPECT_FALSE(sysfs.link_name("bus/pci/devices/0000:11:00.0/physfn").has_value());
}

TEST(Sysfs, ALeadingSlashStaysInsideTheRoot) {
    const SysfsReader sysfs = fake_root();
    EXPECT_EQ(sysfs.read("/class/dmi/id/sys_vendor"), "Amazon EC2");
}
