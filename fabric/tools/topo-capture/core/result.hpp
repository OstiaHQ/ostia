#pragma once

#include <string>
#include <utility>
#include <variant>

namespace ostia::topo_capture {

// gcc 11 has no <expected>. The error is a values-free code, never a raw identifier.
template <typename T> class Result {
  public:
    Result(T value) : _state(std::move(value)) {}
    static Result failure(std::string code) { return Result(Failure{std::move(code)}); }

    [[nodiscard]] bool ok() const { return std::holds_alternative<T>(_state); }
    [[nodiscard]] const T& value() const { return std::get<T>(_state); }
    [[nodiscard]] const std::string& error() const { return std::get<Failure>(_state).code; }

  private:
    struct Failure {
        std::string code;
    };
    explicit Result(Failure failure) : _state(std::move(failure)) {}

    std::variant<T, Failure> _state;
};

} // namespace ostia::topo_capture
