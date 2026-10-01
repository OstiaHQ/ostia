import json

import pytest
from ostia_dev.bench.oracles import ORACLES, OracleError, agree, main

MIB = 1 << 20

NVBANDWIDTH = """\
nvbandwidth Version: v0.7
Built from Git version: v0.7

Device 0: NVIDIA A100-SXM4-80GB (00000000:07:00)
Device 1: NVIDIA A100-SXM4-80GB (00000000:0a:00)

Running device_to_device_memcpy_write_ce.
memcpy CE GPU(row) -> GPU(column) bandwidth (GB/s)
           0         1
 0       N/A    264.30
 1    262.10       N/A

SUM device_to_device_memcpy_write_ce 526.40
"""

IB_WRITE_BW = """\
---------------------------------------------------------------------------------------
                    RDMA_Write BW Test
 Dual-port       : OFF          Device         : mlx5_0
 Number of qps   : 1            Transport type : IB
---------------------------------------------------------------------------------------
 #bytes     #iterations    BW peak[Gb/sec]    BW average[Gb/sec]   MsgRate[Mpps]
 1073741824    100              197.53             196.80              0.000023
---------------------------------------------------------------------------------------
"""

UCX_PERFTEST = """\
|    Stage     | # iterations | 50.0%ile | average | overall |  average |  overall | ...
Final:                   100  43528.1  43530.3  43530.3    23524.3   23524.3          23          23
"""

IPERF3 = json.dumps({"end": {"sum_received": {"bits_per_second": 9.4e9}}})


def test_nvbandwidth_reads_the_directed_cell():
    o = ORACLES["p2p_copy"]["nvbandwidth"]
    params = {"bytes": 1024 * MIB, "direction": "0->1", "concurrency": 1}
    assert o.parse(NVBANDWIDTH, params) == pytest.approx(264.30)
    assert o.parse(NVBANDWIDTH, dict(params, direction="1->0")) == pytest.approx(262.10)
    cmd = o.command(params)
    assert cmd[:3] == ["nvbandwidth", "-t", "device_to_device_memcpy_write_ce"]
    assert "1024" in cmd  # buffer size in MiB, matched to the workload


def test_nvbandwidth_refuses_unmatched_parameters():
    o = ORACLES["p2p_copy"]["nvbandwidth"]
    with pytest.raises(OracleError, match="concurrency"):
        o.command({"bytes": MIB, "direction": "0->1", "concurrency": 2})


def test_ib_write_bw_average_in_gigabytes():
    o = ORACLES["rdma_put"]["ib_write_bw"]
    assert o.parse(IB_WRITE_BW, {"bytes": 1024 * MIB}) == pytest.approx(196.80 / 8)
    cmd = o.command({"bytes": 1024 * MIB, "mem": "cuda", "gpu": 0, "nic": "mlx5_0"})
    assert "--use_cuda=0" in cmd and "--report_gbits" in cmd and "1073741824" in cmd


def test_ucx_perftest_uses_mebibytes():
    o = ORACLES["rdma_put"]["ucx_perftest"]
    assert o.parse(UCX_PERFTEST, {"bytes": 1024 * MIB}) == pytest.approx(23524.3 * MIB / 1e9)
    assert o.command({"bytes": MIB, "mem": "cuda"})[-2:] == ["-m", "cuda"]


def test_iperf3_json():
    o = ORACLES["tcp_put"]["iperf3"]
    assert o.parse(IPERF3, {"bytes": MIB}) == pytest.approx(9.4 / 8)


def test_garbage_output_is_an_error():
    with pytest.raises(OracleError, match="no result"):
        ORACLES["rdma_put"]["ib_write_bw"].parse("Couldn't connect\n", {"bytes": MIB})


def test_agreement_is_within_five_percent():
    assert agree(95.5, 100.0)
    assert agree(104.9, 100.0)
    assert not agree(94.0, 100.0)
    assert not agree(106.0, 100.0)


def _results(tmp_path, bench, params, value):
    record = {
        "schema": 1,
        "provenance": {"git_sha": "0", "date": "2026-10-01T00:00:00Z", "run_id": "r"},
        "compat": dict.fromkeys(
            ("gpu", "driver", "cuda", "nic", "build_level", "compiler", "deps"), "x"
        )
        | {"topology": None},
        "bench": bench,
        "params": params,
        "unit": "GB/s",
        "higher_is_better": True,
        "samples": [value] * 10,
    }
    path = tmp_path / "results.jsonl"
    path.write_text(json.dumps(record) + "\n")
    return path


def test_cli_compares_the_calibration_with_the_oracle(tmp_path, capsys):
    params = {"bytes": 1024 * MIB, "direction": "0->1", "concurrency": 1}
    out = tmp_path / "nvbandwidth.txt"
    out.write_text(NVBANDWIDTH)
    good = _results(tmp_path, "p2p_copy", params, 260.0)
    assert main(["--results", str(good), "--oracle", "nvbandwidth", "--output", str(out)]) == 0
    assert "agrees" in capsys.readouterr().out
    bad = _results(tmp_path, "p2p_copy", params, 230.0)
    assert main(["--results", str(bad), "--oracle", "nvbandwidth", "--output", str(out)]) == 1
    assert "differs" in capsys.readouterr().out


def test_cli_prints_the_matched_command(tmp_path, capsys):
    params = {"bytes": 1024 * MIB, "direction": "0->1", "concurrency": 1}
    path = _results(tmp_path, "p2p_copy", params, 260.0)
    assert main(["--results", str(path), "--oracle", "nvbandwidth", "--print-command"]) == 0
    assert capsys.readouterr().out.startswith("nvbandwidth -t device_to_device_memcpy_write_ce")
