#pragma once

#include <filesystem>

#include "topology/source.hpp"

namespace ostia::fabric::topology {

// Replays a fixture directory: hwloc.xml plus nvml.json, nics.json and links.json.
class FixtureSource final : public TopologySource {
  public:
    // Loads and validates eagerly, so a bad fixture fails at construction.
    explicit FixtureSource(const std::filesystem::path& dir);
    Facts facts() const override;

  private:
    Facts facts_;
};

} // namespace ostia::fabric::topology
