#pragma once

#include <string>
#include <utility>
#include <variant>

namespace ostia::fabric::topology::capture {

// gcc 11 has no <expected>. The error is a values-free code, never a raw identifier.
template <typename T> class Result {
  public:
    // Implicit so callers return a plain value or Result::failure(code).
    Result(T value) : state_(std::move(value)) {}
    static Result failure(std::string code) { return Result(Failure{std::move(code)}); }

    [[nodiscard]] bool ok() const { return std::holds_alternative<T>(state_); }
    [[nodiscard]] const T& value() const { return std::get<T>(state_); }
    [[nodiscard]] const std::string& error() const { return std::get<Failure>(state_).code; }

  private:
    struct Failure {
        std::string code;
    };
    explicit Result(Failure failure) : state_(std::move(failure)) {}

    std::variant<T, Failure> state_;
};

} // namespace ostia::fabric::topology::capture
