// Instrumentation macros (RFC-0001 §5). Build-tree header, reached through
// ostia::telemetry_config: public headers never use these macros or include config.h
// (ostia_dev/ci/check_telemetry_macros.py enforces both).
//
// Each macro expands to `if constexpr (OSTIA_TELEMETRY_LEVEL >= N) { ... }`. Below level N
// the arguments are still type-checked but never evaluated, so they must not have side
// effects:
//
//     OSTIA_COUNT(bytes_sent, n);          // fine
//     OSTIA_COUNT(bytes_sent, pop_next()); // wrong: pop_next() does not run in an off build
//
// The macros work at function scope only.
#pragma once

#include <cstdint>

#include <ostia/telemetry/config.h>
#include <ostia/telemetry/handles.hpp>

namespace ostia::telemetry::detail {
// Hooks the macros call. No-ops until the telemetry runtime (RFC-0002, Rollout PR 8)
// gives them counters, trace rings and debug checks.
inline void count(MetricHandle, std::uint64_t) noexcept {}
inline void trace_event(EventHandle, std::uint64_t, std::uint32_t) noexcept {}
inline void debug_check(bool, const char*, const char*, int) noexcept {}
} // namespace ostia::telemetry::detail

// Adds n to a counter (level metrics and above).
#define OSTIA_COUNT(handle, n)                                                                     \
    do {                                                                                           \
        if constexpr (OSTIA_TELEMETRY_LEVEL >= 1) {                                                \
            ::ostia::telemetry::detail::count((handle), static_cast<std::uint64_t>(n));            \
        }                                                                                          \
    } while (0)

// Records a fine-grained trace event (level trace and above).
#define OSTIA_TRACE_EVENT(handle, arg0, arg1)                                                      \
    do {                                                                                           \
        if constexpr (OSTIA_TELEMETRY_LEVEL >= 2) {                                                \
            ::ostia::telemetry::detail::trace_event((handle), static_cast<std::uint64_t>(arg0),    \
                                                    static_cast<std::uint32_t>(arg1));             \
        }                                                                                          \
    } while (0)

// Checks an invariant (level debug).
#define OSTIA_DEBUG_CHECK(condition, message)                                                      \
    do {                                                                                           \
        if constexpr (OSTIA_TELEMETRY_LEVEL >= 3) {                                                \
            ::ostia::telemetry::detail::debug_check(static_cast<bool>(condition), (message),       \
                                                    __FILE__, __LINE__);                           \
        }                                                                                          \
    } while (0)
