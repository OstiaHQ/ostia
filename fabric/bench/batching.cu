// batching (RFC-0001 §6.4): throughput across message sizes. For each size m, `count`
// messages of m bytes are copied back to back to consecutive offsets on the peer GPU,
// one copy each. The bound for m is m / (t0 + m / B), with t0 the measured time per
// message at the smallest size and B the ceiling (ostia_dev/bench/bounds.py); the gate
// checks 1 MiB and larger.
//
//   batching [--src 0] [--dst 1] [--min 64] [--max 64MiB] [--total 256MiB]
//            [--max-count 16384] [--samples N] [--smoke] [--corrupt]
#include <algorithm>
#include <string>
#include <vector>

#include "common/cuda.cuh"

using namespace ostia::bench;

int main(int argc, char** argv) {
    const Args a(argc, argv);
    const bool smoke = a.flag("smoke");
    if (const int rc = require_devices(a, 2, "batching"); rc != 0) {
        return rc;
    }
    const int src = static_cast<int>(a.num("src", 0));
    const int dst = static_cast<int>(a.num("dst", smoke && device_count() < 2 ? 0 : 1));
    const std::uint64_t min_size = a.size("min", 64);
    const std::uint64_t max_size = a.size("max", smoke ? 4 * kMiB : 64 * kMiB);
    const std::uint64_t total = a.size("total", smoke ? 16 * kMiB : 256 * kMiB);
    const std::uint64_t max_count = a.size("max-count", smoke ? 1024 : 16384);
    if (min_size == 0 || max_size < min_size || total < max_size) {
        return fail("need 0 < --min <= --max <= --total", "for example --min 64 --max 64MiB");
    }
    const bool peer = enable_peer(src, dst);
    DeviceBuffer send(src, total);
    DeviceBuffer recv(dst, total);
    send.fill(4);
    OSTIA_CUDA(cudaSetDevice(src));
    cudaStream_t s{};
    OSTIA_CUDA(cudaStreamCreateWithFlags(&s, cudaStreamNonBlocking));
    EventTimer timer(src);
    const int n = samples(a);
    std::uint64_t moved = 0;

    for (std::uint64_t m = min_size; m <= max_size; m *= 4) {
        const std::uint64_t count = std::max<std::uint64_t>(1, std::min(total / m, max_count));
        OSTIA_CUDA(cudaSetDevice(dst));
        OSTIA_CUDA(cudaMemset(recv.data(), 0, total));
        OSTIA_CUDA(cudaDeviceSynchronize());
        OSTIA_CUDA(cudaSetDevice(src));
        std::vector<double> rates;
        for (int i = 0; i < 1 + n; ++i) {
            timer.start(s);
            for (std::uint64_t k = 0; k < count; ++k) {
                OSTIA_CUDA(
                    cudaMemcpyPeerAsync(recv.data() + k * m, dst, send.data() + k * m, src, m, s));
            }
            timer.stop(s);
            const double t = timer.seconds();
            if (i >= 1) {
                rates.push_back(gbps(count * m, t));
                moved += count * m;
            }
        }
        if (a.flag("corrupt") && m == max_size) {
            recv.corrupt(count * m / 2);
        }
        if (const auto bad = recv.mismatches(4, 0, count * m); bad != 0) {
            return checksum_failed("batching", bad, count * m);
        }
        emit("batching",
             "{\"bytes\": " + std::to_string(m) + ", \"count\": " + std::to_string(count) + "}",
             rates);
    }
    evidence("peer_access=" + std::to_string(peer ? 1 : 0) + " src=" + std::to_string(src) +
             " dst=" + std::to_string(dst) + " bytes=" + std::to_string(moved));
    cudaStreamDestroy(s);
    return 0;
}
