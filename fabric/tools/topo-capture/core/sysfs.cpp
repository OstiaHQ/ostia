#include "core/sysfs.hpp"

#include <algorithm>
#include <fstream>
#include <iterator>
#include <system_error>
#include <utility>

namespace ostia::fabric::topology::capture {

SysfsReader::SysfsReader(std::filesystem::path root) : root_(std::move(root)) {}

std::filesystem::path SysfsReader::resolve(std::string_view rel) const {
    // A leading slash would make operator/ discard the root.
    while (!rel.empty() && rel.front() == '/') {
        rel.remove_prefix(1);
    }
    return root_ / std::string(rel);
}

std::optional<std::string> SysfsReader::read_bytes(std::string_view rel) const {
    const std::filesystem::path path = resolve(rel);
    std::error_code ec;
    if (!std::filesystem::is_regular_file(path, ec)) {
        return std::nullopt;
    }
    std::ifstream in(path, std::ios::binary);
    if (!in) {
        return std::nullopt;
    }
    std::string data{std::istreambuf_iterator<char>(in), std::istreambuf_iterator<char>()};
    // A sysfs attribute that errors on read sets badbit.
    if (in.bad()) {
        return std::nullopt;
    }
    return data;
}

std::optional<std::string> SysfsReader::read(std::string_view rel) const {
    std::optional<std::string> data = read_bytes(rel);
    if (!data) {
        return std::nullopt;
    }
    const auto last = data->find_last_not_of(" \t\r\n");
    data->erase(last == std::string::npos ? 0 : last + 1);
    return data;
}

std::vector<std::string> SysfsReader::list(std::string_view rel) const {
    std::vector<std::string> names;
    std::error_code ec;
    std::filesystem::directory_iterator it(resolve(rel), ec);
    if (ec) {
        return names;
    }
    for (const std::filesystem::directory_entry& entry : it) {
        names.push_back(entry.path().filename().string());
    }
    std::ranges::sort(names);
    return names;
}

std::optional<std::string> SysfsReader::link_name(std::string_view rel) const {
    std::error_code ec;
    const std::filesystem::path target = std::filesystem::read_symlink(resolve(rel), ec);
    if (ec) {
        return std::nullopt;
    }
    std::filesystem::path leaf = target.filename();
    // A target written with a trailing slash has an empty filename.
    if (leaf.empty()) {
        leaf = target.parent_path().filename();
    }
    return leaf.string();
}

} // namespace ostia::fabric::topology::capture
