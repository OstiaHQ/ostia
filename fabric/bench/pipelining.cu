// pipelining (RFC-0001 §6.4): chunked, overlapped pack-and-copy against a synchronous
// pack-then-copy. The pack stage gathers 64-byte blocks at a stride of 128 bytes into a
// staging buffer on the source GPU; the transfer stage copies the staging buffer to the
// peer GPU. Records each stage alone (the bound is the slower of the two,
// tools/bench/bounds.py), the synchronous run and the pipelined run.
//
//   pipelining [--src 0] [--dst 1] [--bytes 256MiB] [--chunk 4MiB] [--samples N]
//              [--smoke] [--corrupt]
#include <string>
#include <vector>

#include "common/cuda.cuh"

using namespace ostia::bench;

namespace {

constexpr std::uint64_t kBlock = 64;
constexpr std::uint32_t kSeed = 3;

__host__ __device__ std::uint64_t source_index(std::uint64_t j) {
    return (j / kBlock) * 2 * kBlock + j % kBlock;
}

__global__ void pack_kernel(const std::uint8_t* in, std::uint8_t* out, std::uint64_t begin,
                            std::uint64_t n) {
    for (std::uint64_t i = blockIdx.x * blockDim.x + threadIdx.x; i < n;
         i += static_cast<std::uint64_t>(gridDim.x) * blockDim.x) {
        out[begin + i] = in[source_index(begin + i)];
    }
}

__global__ void verify_packed(const std::uint8_t* out, std::uint64_t n, unsigned long long* bad) {
    unsigned long long local = 0;
    for (std::uint64_t j = blockIdx.x * blockDim.x + threadIdx.x; j < n;
         j += static_cast<std::uint64_t>(gridDim.x) * blockDim.x) {
        local += out[j] != pattern(kSeed, source_index(j)) ? 1 : 0;
    }
    if (local != 0) {
        atomicAdd(bad, local);
    }
}

void pack(const DeviceBuffer& in, DeviceBuffer& staging, std::uint64_t begin, std::uint64_t n,
          cudaStream_t s) {
    pack_kernel<<<kBlocks, kThreads, 0, s>>>(in.data(), staging.data(), begin, n);
    OSTIA_CUDA(cudaGetLastError());
}

} // namespace

int main(int argc, char** argv) {
    const Args a(argc, argv);
    const bool smoke = a.flag("smoke");
    if (const int rc = require_devices(a, 2, "pipelining"); rc != 0) {
        return rc;
    }
    const int src = static_cast<int>(a.num("src", 0));
    const int dst = static_cast<int>(a.num("dst", smoke && device_count() < 2 ? 0 : 1));
    const std::uint64_t bytes = a.size("bytes", smoke ? 64 * kMiB : 256 * kMiB);
    const std::uint64_t chunk = a.size("chunk", 4 * kMiB);
    if (bytes % chunk != 0 || chunk % kBlock != 0) {
        return fail("--bytes must be a multiple of --chunk, and --chunk of 64",
                    "for example --bytes 256MiB --chunk 4MiB");
    }
    const bool peer = enable_peer(src, dst);

    DeviceBuffer in(src, 2 * bytes);
    DeviceBuffer staging(src, bytes);
    DeviceBuffer out(dst, bytes);
    in.fill(kSeed);
    OSTIA_CUDA(cudaSetDevice(src));
    cudaStream_t sp{};
    cudaStream_t sc{};
    OSTIA_CUDA(cudaStreamCreateWithFlags(&sp, cudaStreamNonBlocking));
    OSTIA_CUDA(cudaStreamCreateWithFlags(&sc, cudaStreamNonBlocking));
    const std::uint64_t chunks = bytes / chunk;
    std::vector<cudaEvent_t> packed(chunks);
    for (auto& e : packed) {
        OSTIA_CUDA(cudaEventCreateWithFlags(&e, cudaEventDisableTiming));
    }
    EventTimer timer(src);
    const int n = samples(a);
    const int warmup = 2;

    auto measure = [&](auto&& body, cudaStream_t last) {
        std::vector<double> rates;
        for (int i = 0; i < warmup + n; ++i) {
            OSTIA_CUDA(cudaDeviceSynchronize());
            timer.start(sp);
            body();
            timer.stop(last);
            const double t = timer.seconds();
            if (i >= warmup) {
                rates.push_back(gbps(bytes, t));
            }
        }
        return rates;
    };

    const auto pack_rates = measure([&] { pack(in, staging, 0, bytes, sp); }, sp);
    const auto transfer_rates = measure(
        [&] { OSTIA_CUDA(cudaMemcpyPeerAsync(out.data(), dst, staging.data(), src, bytes, sp)); },
        sp);
    const auto sync_rates = measure(
        [&] {
            pack(in, staging, 0, bytes, sp);
            OSTIA_CUDA(cudaMemcpyPeerAsync(out.data(), dst, staging.data(), src, bytes, sp));
        },
        sp);
    OSTIA_CUDA(cudaSetDevice(dst));
    OSTIA_CUDA(cudaMemset(out.data(), 0, bytes));
    OSTIA_CUDA(cudaDeviceSynchronize());
    OSTIA_CUDA(cudaSetDevice(src));
    const auto pipelined_rates = measure(
        [&] {
            for (std::uint64_t k = 0; k < chunks; ++k) {
                pack(in, staging, k * chunk, chunk, sp);
                OSTIA_CUDA(cudaEventRecord(packed[k], sp));
                OSTIA_CUDA(cudaStreamWaitEvent(sc, packed[k], 0));
                OSTIA_CUDA(cudaMemcpyPeerAsync(out.data() + k * chunk, dst,
                                               staging.data() + k * chunk, src, chunk, sc));
            }
        },
        sc);

    if (a.flag("corrupt")) {
        out.corrupt(bytes / 2);
    }
    OSTIA_CUDA(cudaSetDevice(dst));
    unsigned long long* bad = nullptr;
    OSTIA_CUDA(cudaMalloc(reinterpret_cast<void**>(&bad), sizeof(*bad)));
    OSTIA_CUDA(cudaMemset(bad, 0, sizeof(*bad)));
    verify_packed<<<kBlocks, kThreads>>>(out.data(), bytes, bad);
    OSTIA_CUDA(cudaGetLastError());
    unsigned long long host_bad = 0;
    OSTIA_CUDA(cudaMemcpy(&host_bad, bad, sizeof(host_bad), cudaMemcpyDeviceToHost));
    OSTIA_CUDA(cudaFree(bad));
    if (host_bad != 0) {
        return checksum_failed("pipelining", host_bad, bytes);
    }

    const std::string b = std::to_string(bytes);
    const std::string c = std::to_string(chunk);
    const std::uint64_t moved =
        bytes * (transfer_rates.size() + sync_rates.size() + pipelined_rates.size());
    evidence("peer_access=" + std::to_string(peer ? 1 : 0) + " src=" + std::to_string(src) +
             " dst=" + std::to_string(dst) + " bytes=" + std::to_string(moved));
    emit("pipelining", "{\"mode\": \"pack\", \"bytes\": " + b + "}", pack_rates);
    emit("pipelining", "{\"mode\": \"transfer\", \"bytes\": " + b + "}", transfer_rates);
    emit("pipelining", "{\"mode\": \"sync\", \"bytes\": " + b + ", \"chunk\": " + c + "}",
         sync_rates);
    emit("pipelining", "{\"mode\": \"pipelined\", \"bytes\": " + b + ", \"chunk\": " + c + "}",
         pipelined_rates);
    for (auto& e : packed) {
        cudaEventDestroy(e);
    }
    cudaStreamDestroy(sp);
    cudaStreamDestroy(sc);
    return 0;
}
