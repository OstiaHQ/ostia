// A minimal nvbench benchmark (RFC-0001 §6.1): exercises the driver's nvbench-to-schema
// conversion end to end on a GPU node (ostia-dev remote, RFC-0005), and is the workload
// of the overhead mechanism's self-test (§6.6). OSTIA_BENCH_INJECT_SLOWDOWN=0.03 makes the kernel
// do 3% more work.
#include <cstdint>
#include <cstdlib>
#include <cuda_runtime.h>
#include <nvbench/nvbench.cuh>

namespace {

__global__ void spin(std::uint64_t iterations, std::uint64_t* sink) {
    std::uint64_t x = threadIdx.x;
    for (std::uint64_t i = 0; i < iterations; ++i) {
        x = x * 6364136223846793005ULL + 1442695040888963407ULL;
    }
    if (threadIdx.x == 0 && blockIdx.x == 0) {
        *sink = x;
    }
}

double injected_slowdown() {
    const char* value = std::getenv("OSTIA_BENCH_INJECT_SLOWDOWN");
    return value != nullptr ? std::atof(value) : 0.0;
}

void noop_kernel(nvbench::state& state) {
    const auto base = static_cast<double>(state.get_int64("Iterations"));
    const auto iterations = static_cast<std::uint64_t>(base * (1.0 + injected_slowdown()));
    std::uint64_t* sink = nullptr;
    cudaMalloc(&sink, sizeof(std::uint64_t));
    state.exec([&](nvbench::launch& launch) {
        spin<<<1, 32, 0, launch.get_stream()>>>(iterations, sink);
    });
    cudaFree(sink);
}

} // namespace

NVBENCH_BENCH(noop_kernel).add_int64_axis("Iterations", {1 << 18});
