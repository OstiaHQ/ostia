#include "core/diagnostics.hpp"

namespace ostia::topo_capture {

void Diagnostics::add(std::string_view line) { _lines.emplace_back(line); }

std::string Diagnostics::render() const {
    std::string out;
    for (const std::string& line : _lines) {
        if (!out.empty()) {
            out += '\n';
        }
        out += line;
    }
    return out;
}

} // namespace ostia::topo_capture
