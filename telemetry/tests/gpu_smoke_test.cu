// GPU smoke test (RFC-0001 §4.2): skips without a device, or fails when OSTIA_REQUIRE_GPU=1
// is set, as on GPU nodes of a remote run (RFC-0005 §3.2).
#include <cstdlib>
#include <cstring>
#include <cuda_runtime.h>
#include <gtest/gtest.h>

namespace {
__global__ void write_value(int* out, int value) { *out = value; }
} // namespace

TEST(GpuSmoke, KernelWritesDeviceMemory) {
    int devices = 0;
    const cudaError_t status = cudaGetDeviceCount(&devices);
    if (status != cudaSuccess || devices == 0) {
        const char* require = std::getenv("OSTIA_REQUIRE_GPU");
        if (require != nullptr && std::strcmp(require, "1") == 0) {
            FAIL() << "no CUDA device, and OSTIA_REQUIRE_GPU=1: " << cudaGetErrorString(status);
        }
        GTEST_SKIP() << "no CUDA device: " << cudaGetErrorString(status);
    }
    int* device = nullptr;
    ASSERT_EQ(cudaMalloc(&device, sizeof(int)), cudaSuccess);
    write_value<<<1, 1>>>(device, 42);
    ASSERT_EQ(cudaGetLastError(), cudaSuccess);
    int host = 0;
    ASSERT_EQ(cudaMemcpy(&host, device, sizeof(int), cudaMemcpyDeviceToHost), cudaSuccess);
    EXPECT_EQ(host, 42);
    cudaFree(device);
}
