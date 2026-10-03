#include <filesystem>
#include <gtest/gtest.h>
#include <string>
#include <vector>

#include <ostia/telemetry/config.h>
#include <ostia/telemetry/telemetry.h>

#if defined(__APPLE__)
#include <mach-o/dyld.h>
#else
#include <link.h>
#endif

// The library reports the level it was built with (RFC-0001 §5).
TEST(BuildLevel, ReportsTheConfiguredLevel) {
    EXPECT_EQ(ostia_telemetry_build_level(), OSTIA_TELEMETRY_LEVEL);
}

namespace {
// Paths of every loaded image whose name contains `needle`. Enumerating images, rather
// than dladdr on a function, is not fooled by a non-PIE executable's PLT stub.
std::vector<std::string> loaded_images(const std::string& needle) {
    std::vector<std::string> found;
#if defined(__APPLE__)
    for (uint32_t i = 0; i < _dyld_image_count(); ++i) {
        const std::string name = _dyld_get_image_name(i);
        if (name.find(needle) != std::string::npos) {
            found.push_back(name);
        }
    }
#else
    struct Ctx {
        const std::string* needle;
        std::vector<std::string>* found;
    } ctx{.needle = &needle, .found = &found};
    dl_iterate_phdr(
        [](dl_phdr_info* info, size_t, void* data) {
            auto* c = static_cast<Ctx*>(data);
            const std::string name = info->dlpi_name != nullptr ? info->dlpi_name : "";
            if (name.find(*c->needle) != std::string::npos) {
                c->found->push_back(name);
            }
            return 0;
        },
        &ctx);
#endif
    return found;
}
} // namespace

// ctest must exercise the fresh build, never a copy installed into the pixi environment.
TEST(BuildLevel, LoadedFromBuildTree) {
    const auto images = loaded_images("libostia-telemetry");
    ASSERT_EQ(images.size(), 1u);
    const auto loaded = std::filesystem::canonical(images[0]).parent_path();
    EXPECT_EQ(loaded, std::filesystem::canonical(OSTIA_EXPECT_LIBDIR)) << images[0];
}
