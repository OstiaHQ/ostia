// The instrumentation macros at each build level (RFC-0001 §5).
#include <gtest/gtest.h>

#include <ostia/telemetry/instrument.hpp>
#include <ostia/telemetry/telemetry_catalog.hpp>

namespace cat = ostia::telemetry::catalog;

TEST(Macros, CompileAtEveryLevel) {
    OSTIA_COUNT(cat::selftest_count, 1);
    OSTIA_TRACE_EVENT(cat::selftest, 1, 2);
    OSTIA_DEBUG_CHECK(1 + 1 == 2, "arithmetic");
    SUCCEED();
}

TEST(Macros, ArgumentsAreNotEvaluatedBelowTheirLevel) {
    int calls = 0;
    auto next = [&calls] { return ++calls; };
    OSTIA_COUNT(cat::selftest_count, next()); // ostia-telemetry: args-pure (tests non-evaluation)
    OSTIA_TRACE_EVENT(cat::selftest, next(), 0); // ostia-telemetry: args-pure
    OSTIA_DEBUG_CHECK(next() > 0, "debug");      // ostia-telemetry: args-pure
    const int expected =
        (OSTIA_TELEMETRY_LEVEL >= 1) + (OSTIA_TELEMETRY_LEVEL >= 2) + (OSTIA_TELEMETRY_LEVEL >= 3);
    EXPECT_EQ(calls, expected);
}

TEST(Macros, HandlesAreStorageFreeConstants) {
    static_assert(cat::selftest_count.index == 0);
    static_assert(cat::selftest_count.kind == ostia::telemetry::MetricKind::counter);
    static_assert(cat::selftest.index == 0);
    static_assert(sizeof(ostia::telemetry::MetricHandle) <= 8);
}
