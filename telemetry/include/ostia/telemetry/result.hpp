// ostia::Result<T, E>: a value or an error, for C++20 where std::expected is missing.
// Member names mirror std::expected so that a later switch is mechanical (RFC-0001 §1.2).
// It lives in ostia-telemetry because telemetry is rank 0, the one component every
// other component may include (RFC-0001 §3.3).
#pragma once

#include <exception>
#include <optional>
#include <string>
#include <type_traits>
#include <utility>
#include <variant>

// Thrown and caught across shared libraries, so its type information must be visible
// even though the libraries build with -fvisibility=hidden.
#if defined(__GNUC__) || defined(__clang__)
#define OSTIA_TELEMETRY_VISIBLE __attribute__((visibility("default")))
#else
#define OSTIA_TELEMETRY_VISIBLE
#endif

namespace ostia {

// The default error type. Codes align with ostia_status when RFC-0002 lands.
struct Error {
    int code = 0;
    std::string message;
};

// Wraps an error to construct an erroneous Result, like std::unexpected.
template <class E> class Unexpected {
  public:
    constexpr explicit Unexpected(E error) : error_(std::move(error)) {}

    constexpr const E& error() const& noexcept { return error_; }
    constexpr E& error() & noexcept { return error_; }
    constexpr E&& error() && noexcept { return std::move(error_); }

  private:
    E error_;
};

template <class E> Unexpected(E) -> Unexpected<E>;

// Thrown by Result::value() when the Result holds an error, like std::bad_expected_access.
class OSTIA_TELEMETRY_VISIBLE BadResultAccess : public std::exception {
  public:
    const char* what() const noexcept override {
        return "ostia::Result: value() called on an error";
    }
};

namespace detail {
template <class T> struct IsUnexpected : std::false_type {};
template <class E> struct IsUnexpected<Unexpected<E>> : std::true_type {};
} // namespace detail

template <class T, class E = Error> class [[nodiscard]] Result {
  public:
    using value_type = T;
    using error_type = E;

    template <class U = T>
        requires(std::is_constructible_v<T, U &&> &&
                 !std::is_same_v<std::remove_cvref_t<U>, Result> &&
                 !detail::IsUnexpected<std::remove_cvref_t<U>>::value)
    constexpr Result(U&& value) : v_(std::in_place_index<0>, std::forward<U>(value)) {}

    template <class G>
    constexpr Result(const Unexpected<G>& u) : v_(std::in_place_index<1>, u.error()) {}

    template <class G>
    constexpr Result(Unexpected<G>&& u) : v_(std::in_place_index<1>, std::move(u).error()) {}

    constexpr bool has_value() const noexcept { return v_.index() == 0; }
    constexpr explicit operator bool() const noexcept { return has_value(); }

    constexpr T& value() & {
        check();
        return std::get<0>(v_);
    }
    constexpr const T& value() const& {
        check();
        return std::get<0>(v_);
    }
    constexpr T&& value() && {
        check();
        return std::move(std::get<0>(v_));
    }
    constexpr const T&& value() const&& {
        check();
        return std::move(std::get<0>(v_));
    }

    // Like std::expected, error() and the dereference operators do not check.
    constexpr E& error() & noexcept { return *std::get_if<1>(&v_); }
    constexpr const E& error() const& noexcept { return *std::get_if<1>(&v_); }
    constexpr E&& error() && noexcept { return std::move(*std::get_if<1>(&v_)); }

    constexpr T& operator*() & noexcept { return *std::get_if<0>(&v_); }
    constexpr const T& operator*() const& noexcept { return *std::get_if<0>(&v_); }
    constexpr T&& operator*() && noexcept { return std::move(*std::get_if<0>(&v_)); }
    constexpr T* operator->() noexcept { return std::get_if<0>(&v_); }
    constexpr const T* operator->() const noexcept { return std::get_if<0>(&v_); }

    template <class U> constexpr T value_or(U&& fallback) const& {
        return has_value() ? **this : static_cast<T>(std::forward<U>(fallback));
    }
    template <class U> constexpr T value_or(U&& fallback) && {
        return has_value() ? std::move(**this) : static_cast<T>(std::forward<U>(fallback));
    }

  private:
    constexpr void check() const {
        if (!has_value()) {
            throw BadResultAccess{};
        }
    }

    // Addressed by index, never by type, so that T and E may be the same type.
    std::variant<T, E> v_;
};

template <class E> class [[nodiscard]] Result<void, E> {
  public:
    using value_type = void;
    using error_type = E;

    constexpr Result() noexcept = default;

    template <class G>
    constexpr Result(const Unexpected<G>& u) : error_(std::in_place, u.error()) {}

    template <class G>
    constexpr Result(Unexpected<G>&& u) : error_(std::in_place, std::move(u).error()) {}

    constexpr bool has_value() const noexcept { return !error_.has_value(); }
    constexpr explicit operator bool() const noexcept { return has_value(); }

    constexpr void value() const {
        if (!has_value()) {
            throw BadResultAccess{};
        }
    }

    constexpr E& error() & noexcept { return *error_; }
    constexpr const E& error() const& noexcept { return *error_; }
    constexpr E&& error() && noexcept { return std::move(*error_); }

  private:
    std::optional<E> error_;
};

} // namespace ostia
