import json

import pytest

from tools.bench.evidence import (
    build,
    check,
    ib_counters,
    main,
    nvlink_counters,
    program_facts,
    ucx_lanes,
)

GIB = 1 << 30

EP_INFO_IB = """\
#
# UCP endpoint 0x7f
#
#               peer: <no debug data>
#                 lane[0]:  8:rc_mlx5/mlx5_0:1.0 md[4] -> md[4]/ib/sysdev[255] rma_bw#0 am am_bw#0
#                 lane[1]:  9:rc_mlx5/mlx5_1:1.0 md[5] -> md[5]/ib/sysdev[255] rma_bw#1 am_bw#1
#                 lane[2]:  2:cuda_copy/cuda md[2] -> md[2]/cuda/sysdev[255] rma#0
#
"""

EP_INFO_TCP = (
    "#                 lane[0]:  3:tcp/lo md[1]  -> md[1]/tcp/sysdev[255] rma_bw#0 am am_bw#0\n"
)

NVLINK = """\
GPU 0: NVIDIA A100-SXM4-80GB (UUID: GPU-aaaa)
\t Link 0: Data Tx: 1048576 KiB
\t Link 0: Data Rx: 0 KiB
\t Link 1: Data Tx: 1048576 KiB
\t Link 1: Data Rx: 0 KiB
GPU 1: NVIDIA A100-SXM4-80GB (UUID: GPU-bbbb)
\t Link 0: Data Tx: 0 KiB
\t Link 0: Data Rx: 1048576 KiB
"""


def _sysfs(root, counters):
    for (dev, port), (xmit, rcv) in counters.items():
        d = root / dev / "ports" / str(port) / "counters"
        d.mkdir(parents=True)
        # the kernel counts these in units of 4 bytes
        (d / "port_xmit_data").write_text(f"{xmit // 4}\n")
        (d / "port_rcv_data").write_text(f"{rcv // 4}\n")


def test_ucx_lanes():
    assert ucx_lanes(EP_INFO_IB) == [
        {"tl": "rc_mlx5", "device": "mlx5_0:1"},
        {"tl": "rc_mlx5", "device": "mlx5_1:1"},
        {"tl": "cuda_copy", "device": "cuda"},
    ]
    assert ucx_lanes(EP_INFO_TCP) == [{"tl": "tcp", "device": "lo"}]


def test_ib_counters_are_bytes(tmp_path):
    _sysfs(tmp_path, {("mlx5_0", 1): (400, 800)})
    assert ib_counters(tmp_path) == {"mlx5_0:1": 1200}
    assert ib_counters(tmp_path / "missing") == {}


def test_nvlink_counters_are_bytes():
    assert nvlink_counters(NVLINK) == {"0/0": GIB, "0/1": GIB, "1/0": GIB}


def test_program_facts():
    out = "ok\nevidence: memory_type=cuda\nevidence: bytes=1024 peer_access=1\n"
    assert program_facts(out) == {"memory_type": "cuda", "bytes": "1024", "peer_access": "1"}


def test_rdma_put_needs_an_ib_lane_gpu_memory_and_nic_traffic():
    before = {"nic": {"mlx5_0:1": 0}, "nvlink": {}}
    after = {"nic": {"mlx5_0:1": GIB}, "nvlink": {}}
    out = EP_INFO_IB + f"evidence: memory_type=cuda bytes={GIB}\n"
    ev = build("rdma_put", before, after, out)
    assert ev["nic_bytes"] == {"mlx5_0:1": GIB}
    assert check(ev) == []
    host = build(
        "rdma_put", before, after, EP_INFO_IB + f"evidence: memory_type=host bytes={GIB}\n"
    )
    assert any("memory type" in p for p in check(host))
    tcp = build(
        "rdma_put", before, after, EP_INFO_TCP + f"evidence: memory_type=cuda bytes={GIB}\n"
    )
    assert any("InfiniBand" in p for p in check(tcp))
    idle = build("rdma_put", before, before, out)
    assert any("traffic" in p for p in check(idle))


def test_dual_link_rails_needs_two_nics_carrying_traffic():
    before = {"nic": {"mlx5_0:1": 0, "mlx5_1:1": 0}, "nvlink": {}}
    out = EP_INFO_IB + f"evidence: mode=rails memory_type=cuda bytes={2 * GIB}\n"
    both = {"nic": {"mlx5_0:1": GIB, "mlx5_1:1": GIB}, "nvlink": {}}
    assert check(build("dual_link", before, both, out)) == []
    one = {"nic": {"mlx5_0:1": 2 * GIB, "mlx5_1:1": 0}, "nvlink": {}}
    assert any("two NICs" in p for p in check(build("dual_link", before, one, out)))


def test_nvlink_workloads_need_peer_access_and_link_traffic():
    before = {"nic": {}, "nvlink": {"0/0": 0, "0/1": 0}}
    after = {"nic": {}, "nvlink": {"0/0": GIB, "0/1": 0}}
    out = f"evidence: peer_access=1 bytes={GIB}\n"
    assert check(build("p2p_copy", before, after, out)) == []
    assert any("peer access" in p for p in check(build("p2p_copy", before, after, "")))
    assert any("NVLink" in p for p in check(build("pipelining", before, before, out)))
    dual = build("dual_link", before, after, f"evidence: mode=nvlink peer_access=1 bytes={GIB}\n")
    assert any("two NVLink" in p for p in check(dual))


def test_tcp_put_needs_a_tcp_lane():
    ev = build("tcp_put", {"nic": {}, "nvlink": {}}, {"nic": {}, "nvlink": {}}, EP_INFO_TCP)
    assert check(ev) == []
    assert check(build("tcp_put", {"nic": {}, "nvlink": {}}, {"nic": {}, "nvlink": {}}, EP_INFO_IB))


def test_cli(tmp_path, capsys):
    ev = build("tcp_put", {"nic": {}, "nvlink": {}}, {"nic": {}, "nvlink": {}}, EP_INFO_IB)
    path = tmp_path / "tcp_put.json"
    path.write_text(json.dumps(ev))
    assert main([str(path)]) == 1
    assert "error: tcp_put" in capsys.readouterr().out
    assert main([str(tmp_path / "missing.json")]) == 1


def _nvsmi(code, out):
    import subprocess

    def run(cmd, **kw):
        if code is None:
            raise FileNotFoundError(cmd[0])
        return subprocess.CompletedProcess(cmd, code, stdout=out, stderr="")

    return run


def test_probe_ib_ok(tmp_path):
    from tools.bench.evidence import probe

    _sysfs(tmp_path, {("mlx5_0", 1): (400, 800)})
    assert probe(["ib"], ib_root=tmp_path) == []


def test_probe_ib_missing(tmp_path):
    from tools.bench.evidence import probe

    [problem] = probe(["ib"], ib_root=tmp_path / "none")
    assert "InfiniBand" in problem and "port_xmit_data" in problem


def test_probe_ib_unreadable(tmp_path):
    import os

    import pytest

    from tools.bench.evidence import probe

    if os.geteuid() == 0:
        pytest.skip("root reads files whatever their mode")
    _sysfs(tmp_path, {("mlx5_0", 1): (400, 800)})
    f = next(tmp_path.glob("*/ports/*/counters/port_xmit_data"))
    f.chmod(0)
    try:
        assert probe(["ib"], ib_root=tmp_path) != []
    finally:
        f.chmod(0o644)


def test_probe_nvlink_ok():
    from tools.bench.evidence import probe

    assert probe(["nvlink"], run=_nvsmi(0, NVLINK)) == []


@pytest.mark.parametrize(("code", "out"), [(0, "GPU 0: NVIDIA L4\n"), (6, ""), (None, "")])
def test_probe_nvlink_no_links(code, out):
    from tools.bench.evidence import probe

    [problem] = probe(["nvlink"], run=_nvsmi(code, out))
    assert "NVLink" in problem


def test_probe_cli_exit_codes(tmp_path, capsys, monkeypatch):
    import tools.bench.evidence as ev

    monkeypatch.setattr(ev, "IB_ROOT", tmp_path / "none")
    assert main(["--probe", "ib"]) == 1
    out = capsys.readouterr().out
    assert "evidence counters unreadable" in out and "fails closed" in out and "RFC-0005 §6" in out
    _sysfs(tmp_path / "sys", {("mlx5_0", 1): (1, 2)})
    monkeypatch.setattr(ev, "IB_ROOT", tmp_path / "sys")
    assert main(["--probe", "ib"]) == 0
