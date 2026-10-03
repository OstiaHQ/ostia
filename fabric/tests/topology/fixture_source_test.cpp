#include <filesystem>
#include <fstream>
#include <gtest/gtest.h>
#include <string>

#include "topology/error.hpp"
#include "topology/fixture_source.hpp"

using namespace ostia::fabric::topology;
namespace fs = std::filesystem;

namespace {

const char* kXml = R"(<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE topology SYSTEM "hwloc2.dtd">
<topology version="2.0">
  <object type="Machine" os_index="0" cpuset="0x00000003" complete_cpuset="0x00000003" allowed_cpuset="@ALLOWED@" nodeset="0x00000001" complete_nodeset="0x00000001" allowed_nodeset="0x00000001" gp_index="1">
    <object type="Package" os_index="0" cpuset="0x00000003" complete_cpuset="0x00000003" nodeset="0x00000001" complete_nodeset="0x00000001" gp_index="2">
      <object type="NUMANode" os_index="0" cpuset="0x00000003" complete_cpuset="0x00000003" nodeset="0x00000001" complete_nodeset="0x00000001" gp_index="3" local_memory="68719476736"/>
      <object type="Core" os_index="0" cpuset="0x00000001" complete_cpuset="0x00000001" nodeset="0x00000001" complete_nodeset="0x00000001" gp_index="4">
        <object type="PU" os_index="0" cpuset="0x00000001" complete_cpuset="0x00000001" nodeset="0x00000001" complete_nodeset="0x00000001" gp_index="5"/>
      </object>
      <object type="Core" os_index="1" cpuset="0x00000002" complete_cpuset="0x00000002" nodeset="0x00000001" complete_nodeset="0x00000001" gp_index="6">
        <object type="PU" os_index="1" cpuset="0x00000002" complete_cpuset="0x00000002" nodeset="0x00000001" complete_nodeset="0x00000001" gp_index="7"/>
      </object>
      <object type="Bridge" gp_index="20" bridge_type="0-1" depth="0" bridge_pci="0000:[00-ff]">
        <object type="PCIDev" gp_index="21" pci_busid="0000:07:00.0" pci_type="0302 [10de:27b8] [10de:16ca] a1" pci_link_speed="31.507692">
          <info name="OstiaPCIeMaxGen" value="4"/>
          <info name="OstiaPCIeMaxWidth" value="16"/>
        </object>
      </object>
    </object>
  </object>
</topology>
)";

const char* kNics = R"({"schema":1,"rdma_probe":"unavailable","rdma":[],"nics":[]})";

std::string xml_with(const std::string& allowed) {
    std::string xml = kXml;
    xml.replace(xml.find("@ALLOWED@"), 9, allowed);
    return xml;
}

class FixtureDir : public ::testing::Test {
  protected:
    void SetUp() override {
        dir_ = fs::temp_directory_path() /
               (std::string("ostia-fixture-") +
                ::testing::UnitTest::GetInstance()->current_test_info()->name());
        fs::remove_all(dir_);
        fs::create_directories(dir_);
    }
    void TearDown() override { fs::remove_all(dir_); }
    void write(const std::string& name, const std::string& text) {
        std::ofstream(dir_ / name) << text;
    }
    fs::path dir_;
};

} // namespace

TEST_F(FixtureDir, Loads) {
    write("hwloc.xml", xml_with("0x00000003"));
    write("nics.json", kNics);
    const Facts f = FixtureSource(dir_).facts();
    EXPECT_EQ(f.packages, 1);
    EXPECT_EQ(f.numa_nodes, std::vector<int>{0});
    EXPECT_TRUE(f.nvml.is_null());
    ASSERT_EQ(f.pci.size(), 2u);
    EXPECT_TRUE(f.pci[0].bridge);
    EXPECT_EQ(f.pci[0].key, "hostbridge-0000:00");
    EXPECT_EQ(f.pci[1].bus_id, "0000:07:00.0");
    EXPECT_EQ(f.pci[1].parent_key, "hostbridge-0000:00");
    EXPECT_EQ(f.pci[1].pci_class, 0x0302);
    EXPECT_EQ(f.pci[1].pci_vendor, 0x10de);
    EXPECT_EQ(f.pci[1].max_gen, 4);
    EXPECT_EQ(f.pci[1].max_width, 16);
    EXPECT_EQ(f.pci[1].numa, std::vector<int>{0});
}

TEST_F(FixtureDir, DisallowedPuIsIncluded) {
    write("hwloc.xml", xml_with("0x00000001"));
    write("nics.json", kNics);
    const Facts f = FixtureSource(dir_).facts();
    EXPECT_EQ(f.pus, 2);
    EXPECT_EQ(f.packages, 1);
}

TEST_F(FixtureDir, MalformedXmlThrowsXml) {
    write("hwloc.xml", xml_with("0x00000003").substr(0, 400));
    write("nics.json", kNics);
    try {
        FixtureSource source(dir_);
        FAIL();
    } catch (const TopologyError& e) {
        EXPECT_EQ(e.code, "xml");
    }
}

TEST_F(FixtureDir, MissingNicsThrows) {
    write("hwloc.xml", xml_with("0x00000003"));
    try {
        FixtureSource source(dir_);
        FAIL();
    } catch (const TopologyError& e) {
        EXPECT_EQ(e.code, "missing_file");
        EXPECT_EQ(e.file, "nics.json");
    }
}

TEST_F(FixtureDir, OptionalFileThatCannotBeReadThrows) {
    write("hwloc.xml", xml_with("0x00000003"));
    write("nics.json", kNics);
    fs::create_directory(dir_ / "nvml.json");
    try {
        FixtureSource source(dir_);
        FAIL();
    } catch (const TopologyError& e) {
        EXPECT_EQ(e.code, "unreadable");
        EXPECT_EQ(e.file, "nvml.json");
    }
}

TEST_F(FixtureDir, UnknownSchemaVersionThrows) {
    write("hwloc.xml", xml_with("0x00000003"));
    write("nics.json", R"({"schema":2,"rdma_probe":"unavailable","rdma":[],"nics":[]})");
    try {
        FixtureSource source(dir_);
        FAIL();
    } catch (const TopologyError& e) {
        EXPECT_EQ(e.code, "schema");
        EXPECT_NE(std::string(e.what()).find("supported: 1"), std::string::npos);
    }
}
