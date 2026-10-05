#include "core/diagnostics.hpp"

namespace ostia::fabric::topology::capture {

void Diagnostics::add(std::string_view line) { lines_.emplace_back(line); }

std::string Diagnostics::render() const {
    std::string out;
    for (const std::string& line : lines_) {
        if (!out.empty()) {
            out += '\n';
        }
        out += line;
    }
    return out;
}

std::string fixed_ms(std::chrono::steady_clock::duration elapsed) {
    const auto us = std::chrono::duration_cast<std::chrono::microseconds>(elapsed).count();
    constexpr long long kUsPerMs = 1000;
    std::string out = std::to_string(us / kUsPerMs);
    out += '.';
    const std::string frac = std::to_string(us % kUsPerMs);
    out.append(3 - frac.size(), '0');
    out += frac;
    return out;
}

} // namespace ostia::fabric::topology::capture
