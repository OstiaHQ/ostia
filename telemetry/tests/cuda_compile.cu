// Public headers compile as C++20 inside a .cu translation unit (RFC-0001 §1.2). Host code
// only: ostia::Result uses std::variant and std::string, which device code cannot.
#include <ostia/telemetry/result.hpp>
#include <ostia/telemetry/telemetry.h>

namespace {
__global__ void noop_kernel(int* out) {
    if (out != nullptr) {
        *out = 1;
    }
}

ostia::Result<int> host_level() {
    const int level = ostia_telemetry_build_level();
    if (level < 0) {
        return ostia::Unexpected(ostia::Error{level, "invalid level"});
    }
    return level;
}
} // namespace

int ostia_cuda_compile_probe() {
    noop_kernel<<<1, 1>>>(nullptr);
    return host_level().value_or(-1);
}
