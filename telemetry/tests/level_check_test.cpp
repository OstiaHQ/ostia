#include <gtest/gtest.h>

#include <ostia/telemetry/config.h>
#include <ostia/telemetry/level_check.hpp>

using ostia::telemetry::detail::check_build_level;

TEST(LevelCheck, MatchingLevelsPass) { EXPECT_TRUE(check_build_level("ostia-fabric", 3, 3)); }

TEST(LevelCheck, MismatchNamesBothLevels) {
    auto r = check_build_level("ostia-fabric", 3, 0);
    ASSERT_FALSE(r);
    const std::string& m = r.error().message;
    EXPECT_NE(m.find("ostia-fabric was built with telemetry level debug (3)"), std::string::npos)
        << m;
    EXPECT_NE(m.find("built with off (0)"), std::string::npos) << m;
    EXPECT_NE(m.find("RFC-0001 §5"), std::string::npos) << m;
}

TEST(LevelCheck, AgainstTheLoadedLibrary) {
    EXPECT_TRUE(ostia::telemetry::detail::check_build_level("test", OSTIA_TELEMETRY_LEVEL));
}
