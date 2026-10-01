// The telemetry flavour check (RFC-0001 §5). Build-tree header (telemetry/instrument/),
// never installed: it adds no exported symbol. A component records the level it was
// compiled at and checks it against the loaded libostia-telemetry when it initialises;
// a mismatch is an error status, never the end of the host process.
#pragma once

#include <dlfcn.h>
#include <string>
#include <string_view>

#include <ostia/telemetry/result.hpp>
#include <ostia/telemetry/telemetry.h>

namespace ostia::telemetry::detail {

inline const char* level_name(int level) {
    switch (level) {
    case 0:
        return "off";
    case 1:
        return "metrics";
    case 2:
        return "trace";
    case 3:
        return "debug";
    default:
        return "unknown";
    }
}

inline std::string loaded_library_path() {
    Dl_info info{};
    if (dladdr(reinterpret_cast<void*>(&ostia_telemetry_build_level), &info) != 0 &&
        info.dli_fname != nullptr) {
        return info.dli_fname;
    }
    return "libostia-telemetry";
}

// Compares a component's compiled level with the loaded library's level.
inline Result<void> check_build_level(std::string_view component, int compiled, int loaded) {
    if (compiled == loaded) {
        return {};
    }
    std::string m(component);
    m += " was built with telemetry level ";
    m += level_name(compiled);
    m += " (" + std::to_string(compiled) + "), but the loaded libostia-telemetry (";
    m += loaded_library_path();
    m += ") was built with ";
    m += level_name(loaded);
    m += " (" + std::to_string(loaded) + ")";
    m += "; a telemetry flavour applies to the whole installed stack; "
         "fix: install one flavour (pixi run ostia-dev py-dev); see: RFC-0001 §5";
    return Unexpected(Error{1, std::move(m)});
}

inline Result<void> check_build_level(std::string_view component, int compiled) {
    return check_build_level(component, compiled, ostia_telemetry_build_level());
}

} // namespace ostia::telemetry::detail
