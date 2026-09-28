// UCX pieces of the gate workload programs (RFC-0001 §6.4): rdma_put, gdr_stream and
// tcp_put, and dual_link's two-rail mode. Rank 0 (the target) exposes a buffer; rank 1
// (the source) puts into it and measures; rank 0 then checksums what arrived.
//
// Rendezvous is one TCP connection. Under fabric/tests/multiprocess/launcher.py (--rank,
// --size, --dir) rank 0 listens on 127.0.0.1 and writes its port to <dir>/port. Across
// nodes, rank 0 takes --listen PORT and rank 1 --connect HOST:PORT.
#pragma once

#include <cstdint>
#include <functional>
#include <memory>
#include <string>
#include <vector>

#include "bench.hpp"

namespace ostia::bench::ucx {

// The defaults that make a program rdma_put, gdr_stream or tcp_put.
struct PutWorkload {
    std::string bench;
    std::string mem;     // default --mem: "cuda" or "host"
    std::string tls;     // forced UCX_TLS, or empty to keep the environment's
    std::uint64_t chunk; // default --chunk; 0 puts the whole buffer at once
    int inflight;        // default --inflight
    bool stream_params;  // record chunk and inflight in the result parameters
};

// One source-to-target transfer of --bytes per sample, with --chunk sized puts and at
// most --inflight outstanding (RFC-0001 §6.4 rdma_put, gdr_stream, tcp_put).
int put_main(int argc, char** argv, const PutWorkload& workload);

// dual_link across nodes: the same transfer over two rails at once, one UCX context per
// NIC (--nics mlx5_0:1,mlx5_1:1). Records path a, path b and both.
int rails_main(int argc, char** argv);

} // namespace ostia::bench::ucx
