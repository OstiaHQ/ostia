#pragma once

#include <filesystem>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

namespace ostia::fabric::topology::capture {

// Every read is relative to the root, so tests and `--sysfs-root` swap in a fake tree and the
// capture never touches the host's /sys by accident.
class SysfsReader {
  public:
    explicit SysfsReader(std::filesystem::path root);

    // Trailing whitespace is trimmed; nullopt when the file is absent or unreadable.
    [[nodiscard]] std::optional<std::string> read(std::string_view rel) const;
    // Unmodified bytes, for binary attributes such as VPD.
    [[nodiscard]] std::optional<std::string> read_bytes(std::string_view rel) const;
    // Entry names in sorted order; empty when the directory is absent.
    [[nodiscard]] std::vector<std::string> list(std::string_view rel) const;
    // Final component of a symlink's target; nullopt when rel is not a symlink.
    [[nodiscard]] std::optional<std::string> link_name(std::string_view rel) const;

  private:
    [[nodiscard]] std::filesystem::path resolve(std::string_view rel) const;

    std::filesystem::path root_;
};

} // namespace ostia::fabric::topology::capture
