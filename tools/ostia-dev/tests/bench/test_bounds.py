import json

import pytest
from ostia_dev.bench.bounds import (
    BoundsError,
    batching_bound,
    check,
    main,
)

MIB = 1 << 20


def rec(bench, params, value, unit="GB/s"):
    return {
        "schema": 1,
        "provenance": {"git_sha": "0", "date": "2026-10-01T00:00:00Z", "run_id": "r"},
        "compat": {
            "gpu": "g",
            "driver": "d",
            "cuda": "12.8",
            "nic": "none",
            "topology": None,
            "build_level": "off",
            "compiler": "c",
            "deps": "x",
        },
        "bench": bench,
        "params": params,
        "unit": unit,
        "higher_is_better": True,
        "samples": [value] * 10,
    }


def test_batching_bound_formula():
    # m / (t0 + m / B): 1 MiB at t0 = 10 us and B = 100 GB/s
    m, t0, b = MIB, 10e-6, 100.0
    assert batching_bound(m, t0, b) == pytest.approx(m / (t0 + m / (b * 1e9)) / 1e9)


def test_pipelining_bound_is_the_slower_stage():
    records = [
        rec("pipelining", {"mode": "pack", "bytes": 256 * MIB}, 50.0),
        rec("pipelining", {"mode": "transfer", "bytes": 256 * MIB}, 40.0),
        rec("pipelining", {"mode": "sync", "bytes": 256 * MIB, "chunk": 4 * MIB}, 22.0),
        rec("pipelining", {"mode": "pipelined", "bytes": 256 * MIB, "chunk": 4 * MIB}, 37.0),
    ]
    [c] = check(records)
    assert (c.bound, c.ok) == (40.0, True)
    assert c.ratio == pytest.approx(37 / 40)
    records[-1]["samples"] = [35.0] * 10
    assert not check(records)[0].ok  # 87.5% < 90%


def test_batching_checks_only_one_mib_and_larger():
    t0, b = 5e-6, 50.0
    sizes = [64, 4096, 256 * 1024, MIB, 16 * MIB]
    # rate at the smallest size defines t0: 64 B per 5 us
    records = [rec("batching", {"bytes": 64}, 64 / t0 / 1e9)]
    for m in sizes[1:]:
        records.append(rec("batching", {"bytes": m}, 0.95 * batching_bound(m, t0, b)))
    checks = check(records, ceiling=b)
    assert [c.params["bytes"] for c in checks] == [MIB, 16 * MIB]
    assert all(c.ok for c in checks)


def test_batching_ceiling_comes_from_p2p_copy_in_the_same_run():
    records = [
        rec("p2p_copy", {"bytes": 1024 * MIB, "direction": "0->1", "concurrency": 1}, 50.0),
        rec("batching", {"bytes": 64}, 64 / 5e-6 / 1e9),
        rec("batching", {"bytes": MIB}, 0.5 * batching_bound(MIB, 5e-6, 50.0)),
    ]
    [c] = check(records)
    assert not c.ok


def test_dual_link_bound_is_the_sum_or_the_shared_limit():
    records = [
        rec("dual_link", {"path": "a", "bytes": 256 * MIB}, 40.0),
        rec("dual_link", {"path": "b", "bytes": 256 * MIB}, 40.0),
        rec("dual_link", {"path": "both", "bytes": 256 * MIB}, 75.0),
    ]
    [c] = check(records)
    assert (c.bound, c.ok) == (80.0, True)
    records.append(rec("dual_link", {"path": "shared_limit", "bytes": 256 * MIB}, 60.0))
    [c] = check(records)
    assert c.bound == 60.0


def test_gdr_stream_bound_is_the_rdma_put_ceiling():
    records = [
        rec("rdma_put", {"bytes": 64 * MIB, "mem": "cuda"}, 10.0),
        rec("rdma_put", {"bytes": 1024 * MIB, "mem": "cuda"}, 24.0),
        rec("gdr_stream", {"chunk": 4 * MIB, "inflight": 8, "bytes": 1024 * MIB}, 22.0),
    ]
    [c] = check(records)
    assert (c.bound, c.ok) == (24.0, True)


def test_missing_inputs_are_an_error():
    with pytest.raises(BoundsError, match="pipelining.*pack"):
        check([rec("pipelining", {"mode": "pipelined", "bytes": MIB, "chunk": MIB}, 1.0)])
    with pytest.raises(BoundsError, match="ceiling"):
        check([rec("batching", {"bytes": 64}, 1.0), rec("batching", {"bytes": MIB}, 1.0)])


def test_cli_exit_code(tmp_path, capsys):
    good = [
        rec("dual_link", {"path": "a", "bytes": MIB}, 40.0),
        rec("dual_link", {"path": "b", "bytes": MIB}, 40.0),
        rec("dual_link", {"path": "both", "bytes": MIB}, 79.0),
    ]
    path = tmp_path / "results.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in good))
    assert main([str(path)]) == 0
    assert "dual_link" in capsys.readouterr().out
    good[-1]["samples"] = [60.0] * 10
    path.write_text("".join(json.dumps(r) + "\n" for r in good))
    assert main([str(path)]) == 1
    assert "below 90%" in capsys.readouterr().out
