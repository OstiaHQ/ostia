"""Tests for tools/bench/ostia_bench.py (RFC-0001 §6.1)."""

import json
from pathlib import Path

import pytest

from tools.bench.ostia_bench import from_nvbench, main, provenance_and_compat
from tools.bench.schema import validate

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "nvbench_noop.json"
UNMEASURED = FIXTURE.with_name("nvbench_unmeasured.json")


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
        ["run", "--format", "ostia", "--bench", str(prog), "--evidence", "--run-id", "e1"]
        + ["--out", str(out), "--", "--smoke", "--bytes", "1048576"]
    )
    assert rc == 0
    ev = json.loads((out / "e1" / "evidence" / "tcp_put.json").read_text())
    assert ev["workload"] == "tcp_put"
    assert ev["lanes"] == [{"tl": "tcp", "device": "lo"}]
    assert ev["program"]["args"] == "--smoke,--bytes,1048576"
    [record] = [
        json.loads(line) for line in (out / "e1" / "results.jsonl").read_text().splitlines()
    ]
    validate(record)


def test_provenance_prefers_ostia_git_sha(monkeypatch):
    import tools.bench.ostia_bench as ob

    calls = []
    real = ob._run
    monkeypatch.setattr(ob, "_run", lambda cmd: calls.append(cmd) or real(cmd))
    monkeypatch.setenv("OSTIA_GIT_SHA", "3f2a9c1+dirty")
    prov, _ = provenance_and_compat("r1", None)
    assert prov["git_sha"] == "3f2a9c1+dirty"
    assert not [c for c in calls if c[0] == "git"]  # a pod has no .git to ask


def test_provenance_without_the_variable_is_unchanged(monkeypatch):
    import subprocess

    import tools.bench.ostia_bench as ob

    monkeypatch.delenv("OSTIA_GIT_SHA", raising=False)
    prov, _ = provenance_and_compat("r1", None)
    want = (
        subprocess.run(
            ["git", "-C", str(ob.ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        or "unknown"
    )
    assert prov["git_sha"] == want


def test_provenance_takes_a_ref_sha(monkeypatch):
    monkeypatch.setenv("OSTIA_GIT_SHA", "abc123400000")
    assert provenance_and_compat("r1", None)[0]["git_sha"] == "abc123400000"


# What nvbench does on a GPU another tenant holds at its power cap: it exits 0 and writes
# a state with no cold measurement.
THROTTLED = f"""#!/usr/bin/env python3
import shutil, sys
print("Warn: GPU throttled below threshold (1004.97 MHz / 2040.00 MHz) (49% < 75%) on sample 0.")
print("Warn: Cold: no accepted samples recorded in 16.71s walltime.")
shutil.copy({str(UNMEASURED)!r}, sys.argv[sys.argv.index("--json") + 1])
"""


@pytest.fixture
def throttled(tmp_path):
    prog = tmp_path / "noop"
    prog.write_text(THROTTLED)
    prog.chmod(0o755)
    return prog


def _unmeasured_error(err):
    assert "error: nvbench measured nothing for noop_kernel [Device=0 Iterations=262144]" in err
    assert "GPU throttled below threshold" in err and "no accepted samples" in err
    assert "fix:" in err


def test_median_seconds_fails_loudly_on_an_unmeasured_state(throttled, capsys):
    assert main(["median-seconds", "--bench", str(throttled)]) == 1
    out = capsys.readouterr()
    assert out.out == ""  # overhead.py must not read a duration
    _unmeasured_error(out.err)


def test_run_fails_loudly_instead_of_dropping_a_sample(throttled, tmp_path, capsys):
    out = tmp_path / "results"
    rc = main(
        ["run", "--bench", str(throttled), "--runs", "3", "--run-id", "u1"] + ["--out", str(out)]
    )
    assert rc == 1 and not (out / "u1").exists()
    _unmeasured_error(capsys.readouterr().err)


def test_convert_fails_on_an_unmeasured_state(tmp_path, capsys):
    rc = main(["convert", "--run-id", "c1", "--out", str(tmp_path), str(FIXTURE), str(UNMEASURED)])
    assert rc == 1 and not (tmp_path / "c1").exists()
    assert "measured nothing" in capsys.readouterr().err


def test_nvbench_crash_shows_its_output(tmp_path, capsys):
    prog = tmp_path / "crash"
    prog.write_text(
        "#!/bin/sh\necho 'CUDA error: no CUDA-capable device is detected' >&2\nexit 3\n"
    )
    prog.chmod(0o755)
    assert main(["median-seconds", "--bench", str(prog)]) == 1
    err = capsys.readouterr().err
    assert "exited 3" in err and "no CUDA-capable device" in err
