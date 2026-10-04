// dual_link (RFC-0001 §6.4): two paths at once. On one node (--mode nvlink, the
// nvlink-node setup) the source GPU copies to two peer GPUs over their NVLink paths,
// each path alone and then both together. Across nodes (--mode rails, the rdma-pair
// setup) the same transfer runs over two NICs (common/ucx.cpp). The bound is the sum of
// the two paths, or a shared resource's measured limit if lower (ostia_dev/bench/bounds.py).
//
//   dual_link [--mode nvlink] [--src 0] [--dst-a 1] [--dst-b 2] [--bytes 256MiB]
//             [--samples N] [--smoke] [--corrupt]
//   dual_link --mode rails --nics mlx5_0:1,mlx5_1:1 (--listen PORT | --connect HOST:PORT)
//             [--bytes 1GiB] [--mem cuda|host] [--samples N] [--smoke]
#include "common/cuda.cuh"
#ifdef OSTIA_BENCH_UCX
#include "common/ucx.hpp"
#endif

#include <string>
#include <vector>

using namespace ostia::bench;

namespace {

int nvlink_main(const Args& a) {
    const bool smoke = a.flag("smoke");
    if (const int rc = require_devices(a, 3, "dual_link"); rc != 0) {
        return rc;
    }
    const bool one_gpu = smoke && device_count() < 3;
    const int src = static_cast<int>(a.num("src", 0));
    const int dst_a = static_cast<int>(a.num("dst-a", one_gpu ? 0 : 1));
    const int dst_b = static_cast<int>(a.num("dst-b", one_gpu ? 0 : 2));
    const std::uint64_t bytes = a.size("bytes", smoke ? 64 * kMiB : 256 * kMiB);
    const bool peer = enable_peer(src, dst_a) && enable_peer(src, dst_b);

    DeviceBuffer send(src, bytes);
    DeviceBuffer recv_a(dst_a, bytes);
    DeviceBuffer recv_b(dst_b, bytes);
    send.fill(6);
    OSTIA_CUDA(cudaSetDevice(src));
    cudaStream_t sa{};
    cudaStream_t sb{};
    OSTIA_CUDA(cudaStreamCreateWithFlags(&sa, cudaStreamNonBlocking));
    OSTIA_CUDA(cudaStreamCreateWithFlags(&sb, cudaStreamNonBlocking));
    cudaEvent_t b_done{};
    OSTIA_CUDA(cudaEventCreateWithFlags(&b_done, cudaEventDisableTiming));
    EventTimer timer(src);
    std::uint64_t moved = 0;

    auto measure = [&](bool use_a, bool use_b) {
        std::vector<double> rates;
        for (int i = 0; i < 2 + samples(a); ++i) {
            OSTIA_CUDA(cudaDeviceSynchronize());
            timer.start(sa);
            OSTIA_CUDA(cudaStreamWaitEvent(sb, b_done, 0));
            if (use_a) {
                OSTIA_CUDA(cudaMemcpyPeerAsync(recv_a.data(), dst_a, send.data(), src, bytes, sa));
            }
            if (use_b) {
                OSTIA_CUDA(cudaMemcpyPeerAsync(recv_b.data(), dst_b, send.data(), src, bytes, sb));
            }
            OSTIA_CUDA(cudaEventRecord(b_done, sb));
            OSTIA_CUDA(cudaStreamWaitEvent(sa, b_done, 0));
            timer.stop(sa);
            const double t = timer.seconds();
            const std::uint64_t n = bytes * ((use_a ? 1 : 0) + (use_b ? 1 : 0));
            if (i >= 2) {
                rates.push_back(gbps(n, t));
                moved += n;
            }
        }
        return rates;
    };
    OSTIA_CUDA(cudaEventRecord(b_done, sb));
    const auto path_a = measure(true, false);
    const auto path_b = measure(false, true);
    const auto both = measure(true, true);

    if (a.flag("corrupt")) {
        recv_b.corrupt(bytes / 2);
    }
    const auto bad = recv_a.mismatches(6, 0, bytes) + recv_b.mismatches(6, 0, bytes);
    if (bad != 0) {
        return checksum_failed("dual_link", bad, 2 * bytes);
    }
    const std::string paths = std::to_string(src) + "->" + std::to_string(dst_a) + "," +
                              std::to_string(src) + "->" + std::to_string(dst_b);
    evidence("mode=nvlink peer_access=" + std::to_string(peer ? 1 : 0) +
             " src=" + std::to_string(src) + " paths=" + paths + " bytes=" + std::to_string(moved));
    const std::string tail = ", \"bytes\": " + std::to_string(bytes) + ", \"mode\": \"nvlink\"}";
    const std::string devices = devices_json(src, dst_a, dst_b);
    emit("dual_link", "{\"path\": \"a\"" + tail, path_a, devices);
    emit("dual_link", "{\"path\": \"b\"" + tail, path_b, devices);
    emit("dual_link", "{\"path\": \"both\"" + tail, both, devices);
    cudaEventDestroy(b_done);
    cudaStreamDestroy(sa);
    cudaStreamDestroy(sb);
    return 0;
}

} // namespace

int main(int argc, char** argv) {
    const Args a(argc, argv);
    const std::string mode = a.str("mode", "nvlink");
    if (mode == "rails") {
#ifdef OSTIA_BENCH_UCX
        return ucx::rails_main(argc, argv);
#else
        return fail("dual_link --mode rails needs UCX",
                    "build in the cuda-12 or cuda-13 environment, which has UCX");
#endif
    }
    if (mode != "nvlink") {
        return fail("unknown --mode " + mode, "use --mode nvlink or --mode rails");
    }
    return nvlink_main(a);
}
