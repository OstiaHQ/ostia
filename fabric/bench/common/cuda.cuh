// CUDA helpers for the gate workload programs: error checks, device buffers filled with
// and checked against ostia::bench::pattern on the GPU, and peer access.
#pragma once

#include <algorithm>
#include <cctype>
#include <cstdint>
#include <cstdio>
#include <cuda_runtime.h>
#include <string>

#include "bench.hpp"

namespace ostia::bench {

#define OSTIA_CUDA(call)                                                                           \
    do {                                                                                           \
        const cudaError_t err_ = (call);                                                           \
        if (err_ != cudaSuccess) {                                                                 \
            std::fprintf(stderr, "error: %s failed: %s\n  at %s:%d\n  see: RFC-0001 §6.4\n",       \
                         #call, cudaGetErrorString(err_), __FILE__, __LINE__);                     \
            std::exit(1);                                                                          \
        }                                                                                          \
    } while (0)

static __global__ void fill_kernel(std::uint8_t* data, std::uint64_t n, std::uint32_t seed,
                                   std::uint64_t offset) {
    for (std::uint64_t i = blockIdx.x * blockDim.x + threadIdx.x; i < n;
         i += static_cast<std::uint64_t>(gridDim.x) * blockDim.x) {
        data[i] = pattern(seed, offset + i);
    }
}

static __global__ void verify_kernel(const std::uint8_t* data, std::uint64_t n, std::uint32_t seed,
                                     std::uint64_t offset, unsigned long long* bad) {
    unsigned long long local = 0;
    for (std::uint64_t i = blockIdx.x * blockDim.x + threadIdx.x; i < n;
         i += static_cast<std::uint64_t>(gridDim.x) * blockDim.x) {
        local += data[i] != pattern(seed, offset + i) ? 1 : 0;
    }
    if (local != 0) {
        atomicAdd(bad, local);
    }
}

inline constexpr int kBlocks = 1024;
inline constexpr int kThreads = 256;

class DeviceBuffer {
  public:
    DeviceBuffer(int device, std::uint64_t bytes) : device_(device), bytes_(bytes) {
        OSTIA_CUDA(cudaSetDevice(device));
        OSTIA_CUDA(cudaMalloc(reinterpret_cast<void**>(&data_), bytes));
        OSTIA_CUDA(cudaMemset(data_, 0, bytes));
    }
    ~DeviceBuffer() {
        cudaSetDevice(device_);
        cudaFree(data_);
    }
    DeviceBuffer(const DeviceBuffer&) = delete;
    DeviceBuffer& operator=(const DeviceBuffer&) = delete;

    [[nodiscard]] std::uint8_t* data() const { return data_; }
    [[nodiscard]] int device() const { return device_; }
    [[nodiscard]] std::uint64_t bytes() const { return bytes_; }

    void fill(std::uint32_t seed, std::uint64_t offset = 0) {
        OSTIA_CUDA(cudaSetDevice(device_));
        fill_kernel<<<kBlocks, kThreads>>>(data_, bytes_, seed, offset);
        OSTIA_CUDA(cudaGetLastError());
        OSTIA_CUDA(cudaDeviceSynchronize());
    }
    // Mismatches in [begin, begin + n) against the pattern at offset + begin.
    std::uint64_t mismatches(std::uint32_t seed, std::uint64_t begin, std::uint64_t n,
                             std::uint64_t offset = 0) const {
        OSTIA_CUDA(cudaSetDevice(device_));
        unsigned long long* bad = nullptr;
        OSTIA_CUDA(cudaMalloc(reinterpret_cast<void**>(&bad), sizeof(*bad)));
        OSTIA_CUDA(cudaMemset(bad, 0, sizeof(*bad)));
        verify_kernel<<<kBlocks, kThreads>>>(data_ + begin, n, seed, offset + begin, bad);
        OSTIA_CUDA(cudaGetLastError());
        unsigned long long host = 0;
        OSTIA_CUDA(cudaMemcpy(&host, bad, sizeof(host), cudaMemcpyDeviceToHost));
        OSTIA_CUDA(cudaFree(bad));
        return host;
    }
    // --corrupt: one wrong byte, which the checksum must catch.
    void corrupt(std::uint64_t at) {
        OSTIA_CUDA(cudaSetDevice(device_));
        std::uint8_t byte = 0;
        OSTIA_CUDA(cudaMemcpy(&byte, data_ + at, 1, cudaMemcpyDeviceToHost));
        byte ^= 0xffU;
        OSTIA_CUDA(cudaMemcpy(data_ + at, &byte, 1, cudaMemcpyHostToDevice));
    }

  private:
    int device_;
    std::uint64_t bytes_;
    std::uint8_t* data_ = nullptr;
};

inline int device_count() {
    int n = 0;
    return cudaGetDeviceCount(&n) == cudaSuccess ? n : 0;
}

// The device's PCI bus ID as domain:bus:device.function in lowercase hex, with a four-digit
// domain. CUDA writes it as "0000:3B:00.0", and some versions pad the domain to eight
// digits; the form here is the one the topology capture uses to name the same GPU.
inline std::string bus_id(int device) {
    char buf[32] = {};
    OSTIA_CUDA(cudaDeviceGetPCIBusId(buf, static_cast<int>(sizeof(buf)), device));
    std::string id = buf;
    std::transform(id.begin(), id.end(), id.begin(),
                   [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    const auto colon = id.find(':');
    if (colon != std::string::npos && colon > 4) {
        const auto keep = id.find_first_not_of('0');
        id.erase(0, std::min(keep == std::string::npos ? colon : keep, colon - 4));
    }
    return id;
}

// {"src_bus": ..., "dst_bus": ...} for the "devices" field of a record.
inline std::string devices_json(int src, int dst) {
    const std::string src_bus = bus_id(src);
    const std::string dst_bus = bus_id(dst);
    return "{\"src_bus\": \"" + src_bus + "\", \"dst_bus\": \"" + dst_bus + "\"}";
}

// The three-GPU form for dual_link: one source and two destinations.
inline std::string devices_json(int src, int dst_a, int dst_b) {
    const std::string src_bus = bus_id(src);
    const std::string dst_a_bus = bus_id(dst_a);
    const std::string dst_b_bus = bus_id(dst_b);
    return "{\"src_bus\": \"" + src_bus + "\", \"dst_a_bus\": \"" + dst_a_bus +
           "\", \"dst_b_bus\": \"" + dst_b_bus + "\"}";
}

// Enables peer access from `from` to `to`; true when they are distinct peers.
inline bool enable_peer(int from, int to) {
    if (from == to) {
        return false;
    }
    int can = 0;
    OSTIA_CUDA(cudaDeviceCanAccessPeer(&can, from, to));
    if (can == 0) {
        return false;
    }
    OSTIA_CUDA(cudaSetDevice(from));
    const cudaError_t err = cudaDeviceEnablePeerAccess(to, 0);
    if (err == cudaErrorPeerAccessAlreadyEnabled) {
        cudaGetLastError();
    } else {
        OSTIA_CUDA(err);
    }
    return true;
}

// Elapsed seconds of work enqueued between two events on a stream.
class EventTimer {
  public:
    explicit EventTimer(int device) {
        OSTIA_CUDA(cudaSetDevice(device));
        OSTIA_CUDA(cudaEventCreate(&start_));
        OSTIA_CUDA(cudaEventCreate(&stop_));
    }
    ~EventTimer() {
        cudaEventDestroy(start_);
        cudaEventDestroy(stop_);
    }
    EventTimer(const EventTimer&) = delete;
    EventTimer& operator=(const EventTimer&) = delete;
    void start(cudaStream_t s) { OSTIA_CUDA(cudaEventRecord(start_, s)); }
    void stop(cudaStream_t s) { OSTIA_CUDA(cudaEventRecord(stop_, s)); }
    double seconds() {
        OSTIA_CUDA(cudaEventSynchronize(stop_));
        float ms = 0;
        OSTIA_CUDA(cudaEventElapsedTime(&ms, start_, stop_));
        return ms / 1e3;
    }

  private:
    cudaEvent_t start_{};
    cudaEvent_t stop_{};
};

// `needed` GPUs, or --smoke on one: exit code 77 without a device, an error with too few.
inline int require_devices(const Args& a, int needed, const std::string& bench) {
    const int n = device_count();
    if (n == 0) {
        std::printf("skipped: no CUDA device\n");
        return kSkip;
    }
    if (n < needed && !a.flag("smoke")) {
        return fail(bench + " needs " + std::to_string(needed) + " GPUs, found " +
                        std::to_string(n),
                    "run it on the nvlink-node setup (RFC-0004), or pass --smoke on one GPU");
    }
    return 0;
}

} // namespace ostia::bench
