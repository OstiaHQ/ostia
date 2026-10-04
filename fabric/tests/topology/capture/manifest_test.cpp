#include <algorithm>
#include <array>
#include <filesystem>
#include <fstream>
#include <gtest/gtest.h>
#include <iterator>
#include <nlohmann/json.hpp>
#include <stdexcept>
#include <string>
#include <system_error>
#include <unistd.h>
#include <vector>

#include "core/manifest.hpp"
#include "core/writers.hpp"
#include "topology/schema.hpp"

using ostia::fabric::topology::validate;
using ostia::fabric::topology::capture::dump;
using ostia::fabric::topology::capture::error_message;
using ostia::fabric::topology::capture::exit_for;
using ostia::fabric::topology::capture::file_hash;
using ostia::fabric::topology::capture::LeakCheck;
using ostia::fabric::topology::capture::Manifest;
using ostia::fabric::topology::capture::manifest_json;
using ostia::fabric::topology::capture::MissingFile;
using ostia::fabric::topology::capture::Status;
using ostia::fabric::topology::capture::write_manifest_atomic;

namespace {

namespace fs = std::filesystem;
using nlohmann::json;

std::string read_file(const fs::path& path) {
    std::ifstream in(path, std::ios::binary);
    return {std::istreambuf_iterator<char>(in), std::istreambuf_iterator<char>()};
}

fs::path temp_dir(const char* tag) {
    const testing::TestInfo* info = testing::UnitTest::GetInstance()->current_test_info();
    std::string name = "ostia-manifest-";
    name += tag;
    name += '-';
    name += info->name();
    name += '-';
    name += std::to_string(getpid());
    const fs::path dir = fs::path(testing::TempDir()) / name;
    fs::remove_all(dir);
    fs::create_directories(dir);
    return dir;
}

} // namespace

TEST(ExitFor, FollowsThePrecedenceOverEveryCombination) {
    struct Case {
        bool leak_or_schema, other, partial;
        int code;
    };
    const std::array<Case, 8> cases{{
        {.leak_or_schema = false, .other = false, .partial = false, .code = 0},
        {.leak_or_schema = false, .other = false, .partial = true, .code = 2},
        {.leak_or_schema = false, .other = true, .partial = false, .code = 1},
        {.leak_or_schema = false, .other = true, .partial = true, .code = 1},
        {.leak_or_schema = true, .other = false, .partial = false, .code = 3},
        {.leak_or_schema = true, .other = false, .partial = true, .code = 3},
        {.leak_or_schema = true, .other = true, .partial = false, .code = 3},
        {.leak_or_schema = true, .other = true, .partial = true, .code = 3},
    }};
    for (const Case& c : cases) {
        EXPECT_EQ(exit_for(c.leak_or_schema, c.other, c.partial), c.code)
            << c.leak_or_schema << c.other << c.partial;
    }
}

TEST(ManifestJson, CompleteAndFailedFormsValidate) {
    Manifest complete;
    complete.status = Status::complete;
    complete.sanitized = true;
    complete.leak_check = LeakCheck::passed;
    complete.topology_id = "topo1:sha256:" + std::string(64, 'a');
    complete.files["hwloc.xml"] = file_hash("x");
    const json doc = manifest_json(complete);
    EXPECT_TRUE(validate("manifest", doc).empty());
    EXPECT_EQ(doc["schema"], 1);
    EXPECT_EQ(doc["tool_version"], OSTIA_TOOL_VERSION);
    EXPECT_EQ(doc["status"], "complete");
    EXPECT_EQ(doc["leak_check"], "passed");
    EXPECT_EQ(doc["missing"], json::array());

    Manifest failed;
    failed.errors = {"leak"};
    failed.missing = {MissingFile{.file = "nvml.json", .reason = "nvml_init"}};
    const json bad = manifest_json(failed);
    EXPECT_TRUE(validate("manifest", bad).empty());
    EXPECT_EQ(bad["status"], "failed");
    EXPECT_EQ(bad["leak_check"], "not_run");
    EXPECT_TRUE(bad["topology_id"].is_null());
    EXPECT_EQ(bad["files"], json::object());
    EXPECT_EQ(bad["errors"][0]["message"], error_message("leak"));
    EXPECT_EQ(bad["missing"][0]["reason"], "nvml_init");
}

TEST(ManifestJson, EveryErrorCodeHasAFixedMessage) {
    for (const char* code : {"hwloc_load", "xml_reimport", "links_read", "links_schema", "schema",
                             "leak", "output_unreadable", "replay", "write_failed", "internal"}) {
        EXPECT_FALSE(error_message(code).empty()) << code;
    }
    EXPECT_NE(error_message("links_schema").find("supported: 1"), std::string_view::npos);
    EXPECT_THROW((void)error_message("no_such_code"), std::out_of_range);
}

TEST(FileHash, IsSha256OfTheBytes) {
    EXPECT_EQ(file_hash("abc"),
              "sha256:ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
}

TEST(WriteManifestAtomic, LeavesOnlyTheManifest) {
    const fs::path dir = temp_dir("atomic");
    Manifest manifest;
    manifest.errors = {"internal"};
    const json doc = manifest_json(manifest);
    write_manifest_atomic(dir, doc);
    // A second write replaces the first through the same temporary name.
    write_manifest_atomic(dir, doc);
    std::vector<std::string> names;
    for (const fs::directory_entry& entry : fs::directory_iterator(dir)) {
        names.push_back(entry.path().filename().string());
    }
    EXPECT_EQ(names, std::vector<std::string>{"manifest.json"});
    EXPECT_EQ(read_file(dir / "manifest.json"), dump(doc));
    fs::remove_all(dir);
}

TEST(WriteManifestAtomic, ThrowsAndLeavesNoTemporaryWhenTheDirectoryIsMissing) {
    const fs::path dir = temp_dir("missing") / "absent";
    EXPECT_THROW(write_manifest_atomic(dir, manifest_json(Manifest{})), std::system_error);
    EXPECT_FALSE(fs::exists(dir / "manifest.json.tmp"));
    fs::remove_all(dir.parent_path());
}

// The C++ half of the validator agreement (RFC-0003 §4); the Python consumer runs the same
// corpus with jsonschema.
TEST(ManifestCorpus, EveryCaseValidatesAsExpected) {
    const fs::path corpus = fs::path(OSTIA_TOPO_DATA_DIR) / "manifest-corpus";
    std::vector<fs::path> files;
    for (const fs::directory_entry& entry : fs::directory_iterator(corpus)) {
        if (entry.path().extension() == ".json") {
            files.push_back(entry.path());
        }
    }
    std::ranges::sort(files);
    ASSERT_GE(files.size(), 10U);
    int valid = 0;
    int invalid = 0;
    for (const fs::path& file : files) {
        const std::string name = file.filename().string();
        SCOPED_TRACE(name);
        const json wrapper = json::parse(read_file(file));
        const std::string expect = wrapper.at("expect").get<std::string>();
        ASSERT_TRUE(expect == "valid" || expect == "invalid");
        ASSERT_TRUE(name.starts_with(expect + "-"));
        const json manifest = wrapper.contains("manifest_text")
                                  ? json::parse(wrapper["manifest_text"].get<std::string>())
                                  : wrapper.at("manifest");
        const bool passes = validate("manifest", manifest).empty();
        EXPECT_EQ(passes, expect == "valid");
        (passes ? valid : invalid) += 1;
    }
    EXPECT_GT(valid, 0);
    EXPECT_GT(invalid, 0);
}
