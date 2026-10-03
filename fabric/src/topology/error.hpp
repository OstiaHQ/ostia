#pragma once

#include <stdexcept>
#include <string>
#include <utility>

namespace ostia::fabric::topology {

// code is a stable machine-readable tag; file names the fixture so a failing replay is
// attributable.
struct TopologyError : std::runtime_error {
    std::string code;
    std::string file;

    TopologyError(std::string code_, std::string file_, const std::string& detail)
        : std::runtime_error(file_.empty() ? detail + " (" + code_ + ")"
                                           : file_ + ": " + detail + " (" + code_ + ")"),
          code(std::move(code_)), file(std::move(file_)) {}
};

} // namespace ostia::fabric::topology
