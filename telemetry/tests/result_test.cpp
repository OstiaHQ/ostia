#include <gtest/gtest.h>
#include <memory>
#include <string>

#include <ostia/telemetry/result.hpp>

using ostia::Error;
using ostia::Result;
using ostia::Unexpected;

namespace {
Result<int> parse(bool ok) {
    if (ok) {
        return 42;
    }
    return Unexpected(Error{7, "bad input"});
}
} // namespace

TEST(Result, ValueCase) {
    auto r = parse(true);
    EXPECT_TRUE(r.has_value());
    EXPECT_TRUE(static_cast<bool>(r));
    EXPECT_EQ(r.value(), 42);
    EXPECT_EQ(*r, 42);
    EXPECT_EQ(r.value_or(0), 42);
}

TEST(Result, ErrorCase) {
    auto r = parse(false);
    EXPECT_FALSE(r.has_value());
    EXPECT_FALSE(r);
    EXPECT_EQ(r.error().code, 7);
    EXPECT_EQ(r.error().message, "bad input");
    EXPECT_EQ(r.value_or(-1), -1);
    EXPECT_THROW((void)r.value(), ostia::BadResultAccess);
}

TEST(Result, MoveOnlyValue) {
    Result<std::unique_ptr<int>> r = std::make_unique<int>(3);
    std::unique_ptr<int> p = std::move(r).value(); // && overload returns T&&
    EXPECT_EQ(*p, 3);
}

TEST(Result, ConstAccess) {
    const Result<std::string> r = std::string("abc");
    EXPECT_EQ(r.value(), "abc");
    EXPECT_EQ(r->size(), 3u);
}

TEST(Result, SameValueAndErrorType) {
    Result<int, int> ok = 5;
    Result<int, int> bad = Unexpected(9);
    EXPECT_TRUE(ok);
    EXPECT_EQ(*ok, 5);
    EXPECT_FALSE(bad);
    EXPECT_EQ(bad.error(), 9);
}

TEST(Result, Void) {
    Result<void> ok;
    EXPECT_TRUE(ok);
    ok.value();
    Result<void> bad = Unexpected(Error{1, "x"});
    EXPECT_FALSE(bad);
    EXPECT_EQ(bad.error().code, 1);
    EXPECT_THROW(bad.value(), ostia::BadResultAccess);
    Result<void> copy = bad;
    EXPECT_FALSE(copy);
}

TEST(Result, CustomErrorType) {
    Result<int, std::string> r = Unexpected(std::string("e"));
    EXPECT_EQ(r.error(), "e");
}

namespace {
constexpr bool constexpr_ok() {
    Result<int, int> r = 5;
    Result<int, int> e = Unexpected(3);
    return r.has_value() && *r == 5 && !e.has_value() && e.error() == 3;
}
static_assert(constexpr_ok());
} // namespace
