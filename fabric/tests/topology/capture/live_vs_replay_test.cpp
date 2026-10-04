#include <array>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <gtest/gtest.h>
#include <memory>
#include <optional>
#include <string>
#include <system_error>
#include <unistd.h>

#include "core/diagnostics.hpp"
#include "core/pipeline.hpp"
#include "core/sysfs.hpp"
#include "live/live.hpp"
#include "topology/builder.hpp"
#include "topology/fixture_source.hpp"
#include "topology/identity.hpp"
#include "topology/model.hpp"

namespace fs = std::filesystem;
using ostia::fabric::topology::build;
using ostia::fabric::topology::canonical_dump;
using ostia::fabric::topology::FixtureSource;
using ostia::fabric::topology::topo1;
using ostia::fabric::topology::capture::capture;
using ostia::fabric::topology::capture::CaptureOutcome;
using ostia::fabric::topology::capture::Diagnostics;
using ostia::fabric::topology::capture::Inputs;
using ostia::fabric::topology::capture::live_facts;
using ostia::fabric::topology::capture::load_live_topology;
using ostia::fabric::topology::capture::load_nvml;
using ostia::fabric::topology::capture::make_verbs;
using ostia::fabric::topology::capture::MetaFlags;
using ostia::fabric::topology::capture::NvmlApi;
using ostia::fabric::topology::capture::SysfsReader;
using ostia::fabric::topology::capture::TopologyPtr;
using ostia::fabric::topology::capture::VerbsApi;

namespace {

constexpr int kSkip = 77; // SKIP_RETURN_CODE in fabric/tests/CMakeLists.txt

// RFC-0003 §4 sets the capture budget; a probe that hangs fails here rather than in the pod's
// own timeout.
constexpr std::chrono::seconds kCaptureBudget{60};

// The directory goes under $TMPDIR and never under a path a remote run collects.
class ScratchDir {
  public:
    ScratchDir() {
        const char* tmp = std::getenv("TMPDIR");
        std::string pattern = (tmp != nullptr && *tmp != '\0' ? tmp : "/tmp");
        pattern += "/ostia-live-vs-replay-XXXXXX";
        if (::mkdtemp(pattern.data()) != nullptr) {
            path_ = pattern;
        }
    }
    ScratchDir(const ScratchDir&) = delete;
    ScratchDir& operator=(const ScratchDir&) = delete;
    ~ScratchDir() {
        if (!path_.empty()) {
            std::error_code ec;
            fs::remove_all(path_, ec);
        }
    }
    [[nodiscard]] const fs::path& path() const { return path_; }

  private:
    fs::path path_;
};

bool have_gpu() {
    Diagnostics diag;
    const std::unique_ptr<NvmlApi> nvml = load_nvml(diag);
    if (nvml == nullptr) {
        return false;
    }
    const auto facts = nvml->query();
    return facts.ok() && !facts.value().gpus.empty();
}

// The live and the replayed model of one machine must agree on every edge and on the topology id
// (RFC-0003 §2). Inputs are assembled as main.cpp does for a pod: the verbs probe on, /sys as the
// sysfs root, no extra identifiers.
TEST(LiveVsReplay, CaptureReplaysToTheLiveModel) {
    const ScratchDir scratch;
    ASSERT_FALSE(scratch.path().empty()) << "mkdtemp failed";

    Diagnostics diag;
    const TopologyPtr topo = load_live_topology(diag);
    ASSERT_NE(topo, nullptr);
    const SysfsReader sysfs("/sys");
    const std::unique_ptr<NvmlApi> nvml = load_nvml(diag);
    const std::unique_ptr<VerbsApi> verbs = make_verbs(sysfs, diag);

    Inputs in;
    in.topo = topo.get();
    in.nvml = nvml.get();
    in.verbs = verbs.get();
    in.sysfs = &sysfs;
    in.meta = MetaFlags{.provider = "test", .instance_type = "test", .node_index = std::nullopt};
    in.live_host = true;

    const auto live = build(live_facts(in));

    const auto start = std::chrono::steady_clock::now();
    const CaptureOutcome outcome = capture(in, scratch.path() / "capture", diag);
    const auto elapsed = std::chrono::steady_clock::now() - start;
    EXPECT_LT(elapsed, kCaptureBudget);

    ASSERT_TRUE(outcome.exit_code == 0 || outcome.exit_code == 2)
        << "capture exit " << outcome.exit_code;

    const auto replayed = build(FixtureSource(scratch.path() / "capture").facts());
    EXPECT_EQ(canonical_dump(live), canonical_dump(replayed));
    EXPECT_EQ(outcome.topology_id, std::optional<std::string>(topo1(live)));
}

} // namespace

// A remote run's GPU pod sets OSTIA_REQUIRE_GPU=1, where no device is a failure (RFC-0005 §3.2);
// elsewhere the test skips (RFC-0001 §4.2).
int main(int argc, char** argv) {
    if (!have_gpu()) {
        const char* require = std::getenv("OSTIA_REQUIRE_GPU");
        if (require != nullptr && std::strcmp(require, "1") == 0) {
            std::fprintf(stderr, "error: no CUDA device, and OSTIA_REQUIRE_GPU=1\n");
            return 1;
        }
        std::printf("skipped: no CUDA device\n");
        return kSkip;
    }
    ::testing::InitGoogleTest(&argc, argv);
    return RUN_ALL_TESTS();
}
