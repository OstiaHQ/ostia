// p2p_copy (calibration, RFC-0001 §6.4): the NVLink/PCIe ceiling for one large copy
// between two GPUs, one copy-engine copy at a time (cudaMemcpyPeerAsync), device memory
// on both sides. The oracle is nvbandwidth's device_to_device_memcpy_write_ce with the
// same size and direction (ostia_dev/bench/oracles.py).
//
//   p2p_copy [--src 0] [--dst 1] [--bytes 1GiB] [--direction uni|bi] [--samples N]
//            [--smoke] [--corrupt]
#include <string>
#include <vector>

#include "common/cuda.cuh"

using namespace ostia::bench;

int main(int argc, char** argv) {
    const Args a(argc, argv);
    const bool smoke = a.flag("smoke");
    if (const int rc = require_devices(a, 2, "p2p_copy"); rc != 0) {
        return rc;
    }
    const int src = static_cast<int>(a.num("src", 0));
    const int dst = static_cast<int>(a.num("dst", smoke && device_count() < 2 ? 0 : 1));
    const std::uint64_t bytes = a.size("bytes", smoke ? 64 * kMiB : kGiB);
    const bool bi = a.str("direction", "uni") == "bi";
    const bool peer = enable_peer(src, dst) && enable_peer(dst, src);

    DeviceBuffer send(src, bytes);
    DeviceBuffer recv(dst, bytes);
    DeviceBuffer back_send(dst, bi ? bytes : 1);
    DeviceBuffer back_recv(src, bi ? bytes : 1);
    send.fill(1);
    if (bi) {
        back_send.fill(2);
    }
    cudaStream_t s1{};
    cudaStream_t s2{};
    OSTIA_CUDA(cudaSetDevice(src));
    OSTIA_CUDA(cudaStreamCreateWithFlags(&s1, cudaStreamNonBlocking));
    OSTIA_CUDA(cudaStreamCreateWithFlags(&s2, cudaStreamNonBlocking));
    EventTimer timer(src);

    std::vector<double> rates;
    const int warmup = 2;
    for (int i = 0; i < warmup + samples(a); ++i) {
        timer.start(s1);
        OSTIA_CUDA(cudaMemcpyPeerAsync(recv.data(), dst, send.data(), src, bytes, s1));
        if (bi) {
            cudaEvent_t started{};
            OSTIA_CUDA(cudaEventCreate(&started));
            OSTIA_CUDA(cudaEventRecord(started, s1));
            OSTIA_CUDA(cudaStreamWaitEvent(s2, started));
            OSTIA_CUDA(
                cudaMemcpyPeerAsync(back_recv.data(), src, back_send.data(), dst, bytes, s2));
            OSTIA_CUDA(cudaEventRecord(started, s2));
            OSTIA_CUDA(cudaStreamWaitEvent(s1, started));
            OSTIA_CUDA(cudaEventDestroy(started));
        }
        timer.stop(s1);
        const double t = timer.seconds();
        if (i >= warmup) {
            rates.push_back(gbps(bi ? 2 * bytes : bytes, t));
        }
    }
    if (a.flag("corrupt")) {
        recv.corrupt(bytes / 2);
    }
    std::uint64_t bad = recv.mismatches(1, 0, bytes);
    if (bi) {
        bad += back_recv.mismatches(2, 0, bytes);
    }
    if (bad != 0) {
        return checksum_failed("p2p_copy", bad, bi ? 2 * bytes : bytes);
    }
    const std::string direction = std::to_string(src) + (bi ? "<->" : "->") + std::to_string(dst);
    const std::uint64_t moved = (bi ? 2 : 1) * bytes * rates.size();
    evidence("peer_access=" + std::to_string(peer ? 1 : 0) + " src=" + std::to_string(src) +
             " dst=" + std::to_string(dst) + " bytes=" + std::to_string(moved));
    emit("p2p_copy",
         "{\"bytes\": " + std::to_string(bytes) + ", \"direction\": \"" + direction +
             "\", \"concurrency\": 1}",
         rates, devices_json(src, dst));
    OSTIA_CUDA(cudaStreamDestroy(s1));
    OSTIA_CUDA(cudaStreamDestroy(s2));
    return 0;
}
