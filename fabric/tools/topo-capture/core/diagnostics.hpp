#pragma once

#include <string>
#include <string_view>
#include <vector>

namespace ostia::topo_capture {

// Lines name a kind, file, locator or count, never a raw identifier value.
class Diagnostics {
  public:
    void add(std::string_view line);
    [[nodiscard]] const std::vector<std::string>& lines() const { return _lines; }
    [[nodiscard]] std::string render() const;

  private:
    std::vector<std::string> _lines;
};

} // namespace ostia::topo_capture
