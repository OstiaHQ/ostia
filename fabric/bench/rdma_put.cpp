// rdma_put (calibration, RFC-0001 §6.4): the GPUDirect RDMA ceiling for one large put of
// GPU memory to another node, one put of --bytes per sample. The oracle is
// `ib_write_bw --use_cuda` or `ucx_perftest -m cuda` with the same size
// (ostia_dev/bench/oracles.py).
//
//   rdma_put --listen PORT | --connect HOST:PORT | (launcher: --rank R --size 2 --dir D)
//            [--bytes 1GiB] [--mem cuda|host] [--nic mlx5_0:1] [--samples N] [--smoke]
#include "common/ucx.hpp"

int main(int argc, char** argv) {
    return ostia::bench::ucx::put_main(argc, argv, {"rdma_put", "cuda", "", 0, 1, false});
}
