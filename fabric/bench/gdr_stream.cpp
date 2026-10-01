// gdr_stream (RFC-0001 §6.4): a sustained, pipelined GPU-to-GPU stream across nodes, in
// 4 MiB chunks with several in flight. The bound is the rdma_put ceiling measured on the
// same pair (ostia_dev/bench/bounds.py).
//
//   gdr_stream --listen PORT | --connect HOST:PORT | (launcher: --rank R --size 2 --dir D)
//              [--bytes 1GiB] [--chunk 4MiB] [--inflight 8] [--mem cuda|host]
//              [--nic mlx5_0:1] [--samples N] [--smoke]
#include "common/ucx.hpp"

int main(int argc, char** argv) {
    return ostia::bench::ucx::put_main(argc, argv,
                                       {"gdr_stream", "cuda", "", 4 * ostia::bench::kMiB, 8, true});
}
