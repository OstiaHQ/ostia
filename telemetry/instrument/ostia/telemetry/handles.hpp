// Handle types of the generated telemetry catalogs (RFC-0002 §1). Build-tree header: the
// handles are storage-free constants, present in every build level (RFC-0001 §5).
#pragma once

#include <cstdint>

namespace ostia::telemetry {

enum class MetricKind : std::uint8_t { counter, updown, histogram, gauge };

// A metric's index in its component's catalog, and its kind.
struct MetricHandle {
    std::uint32_t index;
    MetricKind kind;
};

// A trace event's index in its component's catalog.
struct EventHandle {
    std::uint16_t index;
};

} // namespace ostia::telemetry
