"""Tests for tools/bench/ostia_bench.py (RFC-0001 §6.1)."""

import json
from pathlib import Path

from tools.bench.ostia_bench import from_nvbench, main, provenance_and_compat
from tools.bench.schema import validate

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "nvbench_noop.json"


def test_nvbench_json_becomes_measurements():
    m = from_nvbench(json.loads(FIXTURE.read_text()))
    assert m[("noop_kernel", '{"Iterations": 65536}')] == ("s", False, 1.25e-05)
    assert m[("copy", '{"Bytes": 1048576}')] == ("GB/s", True, 250.0)
    assert ("noop_kernel", '{"Iterations": 1}') not in m  # skipped states are dropped


def test_records_are_schema_valid(tmp_path):
    runs = [json.loads(FIXTURE.read_text())] * 10
    out = tmp_path / "results"
    main(["convert", "--run-id", "t1", "--out", str(out), *[str(FIXTURE)] * 10])
    lines = (out / "t1" / "results.jsonl").read_text().splitlines()
    records = [json.loads(line) for line in lines]
    for r in records:
        validate(r)
    noop = next(r for r in records if r["bench"] == "noop_kernel")
    assert len(noop["samples"]) == len(runs) and noop["unit"] == "s"
    assert noop["compat"]["topology"] is None  # before RFC-0003 lands (PR 6)


def test_compat_fields_are_complete(tmp_path):
    prov, compat = provenance_and_compat("r1", build_dir=None)
    assert set(prov) == {"git_sha", "date", "run_id"} and prov["run_id"] == "r1"
    assert compat["deps"].startswith("pixi.lock:")


def test_needs_gpu_without_one_is_a_contract_error(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))  # no nvidia-smi
    rc = main(["run", "--needs-gpu", "--bench", "/bin/true", "--out", str(tmp_path)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "error: this benchmark needs a CUDA GPU" in err and "fix:" in err


PROGRAM = """#!/usr/bin/env python3
import json, sys
print("#                 lane[0]:  3:tcp/lo md[1]  -> md[1]/tcp/sysdev[255] rma_bw#0")
print("evidence: memory_type=host bytes=1048576 args=" + ",".join(sys.argv[1:]))
print(json.dumps({"bench": "tcp_put", "params": {"bytes": 1048576}, "unit": "GB/s",
                  "higher_is_better": True, "samples": [1.0] * 10}))
"""


def test_evidence_is_recorded_next_to_the_results(tmp_path):
    prog = tmp_path / "tcp_put"
    prog.write_text(PROGRAM)
    prog.chmod(0o755)
    out = tmp_path / "results"
    rc = main(
        [
            "run",
            "--format",
            "ostia",
            "--bench",
            str(prog),
            "--args",
            "--bytes 1048576",
            "--evidence",
            "--run-id",
            "e1",
            "--out",
            str(out),
        ]
    )
    assert rc == 0
    ev = json.loads((out / "e1" / "evidence" / "tcp_put.json").read_text())
    assert ev["workload"] == "tcp_put"
    assert ev["lanes"] == [{"tl": "tcp", "device": "lo"}]
    assert ev["program"]["args"] == "--bytes,1048576"
    [record] = [
        json.loads(line) for line in (out / "e1" / "results.jsonl").read_text().splitlines()
    ]
    validate(record)
