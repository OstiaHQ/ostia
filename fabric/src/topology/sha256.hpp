#pragma once

#include <string>
#include <string_view>

namespace ostia::fabric::topology {

// 64 lowercase hex digits (FIPS 180-4); used to pin fixture contents.
std::string sha256_hex(std::string_view data);

} // namespace ostia::fabric::topology
