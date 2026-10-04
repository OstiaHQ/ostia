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

} // namespace ostia::fabric::topology::capture
