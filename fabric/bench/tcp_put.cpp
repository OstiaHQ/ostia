// tcp_put (informational, RFC-0001 §6.4): the TCP ceiling between two nodes, host memory,
// UCX restricted to TCP. The oracle is iperf3 or `ucx_perftest` over TCP
// (ostia_dev/bench/oracles.py).
//
//   tcp_put --listen PORT | --connect HOST:PORT | (launcher: --rank R --size 2 --dir D)
//           [--bytes 1GiB] [--nic eth0] [--samples N] [--smoke]
#include "common/ucx.hpp"

int main(int argc, char** argv) {
    return ostia::bench::ucx::put_main(argc, argv, {"tcp_put", "host", "tcp,self", 0, 1, false});
}
