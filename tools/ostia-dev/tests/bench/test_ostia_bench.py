"""Tests for tools/ostia-dev/src/ostia_dev/bench/ostia_bench.py (RFC-0001 §6.1)."""

import json
import shutil
import subprocess
from pathlib import Path

import ostia_dev.bench.ostia_bench as ob
import pytest
from ostia_dev.bench.ostia_bench import from_nvbench, main, provenance_and_compat
from ostia_dev.bench.schema import validate

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
    import ostia_dev.bench.ostia_bench as ob

    calls = []
    real = ob._run
    monkeypatch.setattr(ob, "_run", lambda cmd: calls.append(cmd) or real(cmd))
    monkeypatch.setenv("OSTIA_GIT_SHA", "3f2a9c1+dirty")
    prov, _ = provenance_and_compat("r1", None)
    assert prov["git_sha"] == "3f2a9c1+dirty"
    assert not [c for c in calls if c[0] == "git"]  # a pod has no .git to ask


def test_provenance_without_the_variable_is_unchanged(monkeypatch):
    import subprocess

    import ostia_dev.bench.ostia_bench as ob

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


RECORD = '{"bench": "tcp_put", "params": {"bytes": 16777216}, "unit": "GB/s", '
RECORD += '"higher_is_better": true, "samples": [1.0, 1.1, 1.2]}'


@pytest.fixture
def launched(monkeypatch):
    """Records the program command the driver runs; git and nvidia-smi look absent."""
    import subprocess

    import ostia_dev.bench.ostia_bench as ob

    calls = []

    def fake_run(cmd, **kw):
        if cmd[0] in ("git", "nvidia-smi"):
            raise FileNotFoundError(cmd[0])
        calls.append((cmd, kw.get("env") or {}))
        return subprocess.CompletedProcess(cmd, 0, stdout=RECORD + "\n", stderr="")

    monkeypatch.setattr(ob.subprocess, "run", fake_run)
    return calls


def _ostia_run(tmp_path, *flags):
    argv = ["run", "--format", "ostia", "--bench", "/b/tcp_put", "--runs", "3", "--run-id", "t"]
    return main([*argv, "--out", str(tmp_path), *flags, "--", "--smoke"])


def test_launcher_argv_without_ranks(launched, tmp_path):
    assert _ostia_run(tmp_path) == 0
    [(cmd, env)] = launched
    assert cmd == ["/b/tcp_put", "--smoke"]
    assert env["OSTIA_BENCH_RUNS"] == "3"


def test_launcher_argv_with_ranks(launched, tmp_path):
    import sys

    from ostia_dev.bench.ostia_bench import ROOT

    assert _ostia_run(tmp_path, "--ranks", "2") == 0
    [(cmd, _)] = launched
    launcher = str(ROOT / "fabric" / "tests" / "multiprocess" / "launcher.py")
    assert cmd == [sys.executable, launcher, "--ranks", "2", "--", "/b/tcp_put", "--smoke"]


def test_launcher_argv_with_ranks_and_tls(launched, tmp_path):
    assert _ostia_run(tmp_path, "--ranks", "2", "--tls", "cuda_ipc,tcp,self") == 0
    [(cmd, _)] = launched
    assert cmd[2:9] == ["--ranks", "2", "--tls", "cuda_ipc,tcp,self", "--expect", "", "--"]
    assert cmd[9:] == ["/b/tcp_put", "--smoke"]


def test_launcher_records_get_provenance(launched, tmp_path):
    assert _ostia_run(tmp_path, "--ranks", "2") == 0
    [record] = [
        json.loads(line) for line in (tmp_path / "t" / "results.jsonl").read_text().splitlines()
    ]
    assert record["schema"] == 1 and record["provenance"]["run_id"] == "t"
    assert set(record["compat"]) >= {"gpu", "driver", "deps"}
    validate(record)


PEER = {"OSTIA_SIZE": "2", "OSTIA_PEER_HOST": "j-0.j", "OSTIA_PORT": "29400"}


@pytest.fixture
def rank(monkeypatch):
    import ostia_dev.bench.ostia_bench as ob

    monkeypatch.setattr(ob, "_barrier", lambda r, host, port: None)

    def set_rank(r, **env):
        for k, v in {**PEER, "OSTIA_RANK": str(r), **env}.items():
            if v is None:
                monkeypatch.delenv(k, raising=False)
            else:
                monkeypatch.setenv(k, v)

    return set_rank


@pytest.fixture
def dns(monkeypatch):
    """A resolver that fails `failures` times; sleep advances a fake clock."""
    import socket

    import ostia_dev.bench.ostia_bench as ob

    state = {"failures": 0, "t": 0.0, "sleeps": 0, "lookups": 0}

    def resolve(host, port, *a, **kw):
        state["lookups"] += 1
        if state["failures"] is None or state["lookups"] <= state["failures"]:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", int(port)))]

    def sleep(s):
        state["sleeps"] += 1
        state["t"] += s

    monkeypatch.setattr(ob.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(ob.time, "sleep", sleep)
    monkeypatch.setattr(ob.time, "monotonic", lambda: state["t"])
    return state


def test_remote_rank0_listens(launched, rank, dns, tmp_path):
    rank(0)
    assert _ostia_run(tmp_path, "--remote") == 0
    [(cmd, _)] = launched
    assert cmd == ["/b/tcp_put", "--smoke", "--listen", "29400"]
    assert not (tmp_path / "t").exists()  # the target prints no record
    assert dns["lookups"] == 0


def test_remote_rank1_retries_dns_then_connects(launched, rank, dns, tmp_path):
    rank(1)
    dns["failures"] = 3
    assert _ostia_run(tmp_path, "--remote") == 0
    [(cmd, _)] = launched
    assert cmd == ["/b/tcp_put", "--smoke", "--connect", "j-0.j:29400"]
    assert dns["sleeps"] == 3
    assert (tmp_path / "t" / "results.jsonl").exists()


def test_remote_rank1_gives_up_after_two_minutes(launched, rank, dns, tmp_path, capsys):
    rank(1)
    dns["failures"] = None
    assert _ostia_run(tmp_path, "--remote") == 1
    assert launched == []
    err = capsys.readouterr().err
    assert "error: rank 1 cannot resolve its peer j-0.j" in err
    assert "see: RFC-0005 §4.11" in err
    assert 118 <= dns["t"] <= 122


@pytest.mark.parametrize(
    ("env", "named"),
    [
        ({"OSTIA_RANK": None}, "OSTIA_RANK"),
        ({"OSTIA_SIZE": "3"}, "OSTIA_SIZE"),
        ({"OSTIA_RANK": "2"}, "OSTIA_RANK"),
        ({"OSTIA_PORT": None}, "OSTIA_PORT"),
    ],
)
def test_remote_requires_rank_env(launched, rank, dns, tmp_path, capsys, env, named):
    rank(0, **env)
    assert _ostia_run(tmp_path, "--remote") == 2
    assert launched == []
    assert named in capsys.readouterr().err


def test_remote_refuses_ranks_and_nvbench(launched, rank, tmp_path, capsys):
    rank(0)
    assert _ostia_run(tmp_path, "--remote", "--ranks", "2") == 2
    argv = ["run", "--remote", "--bench", "/b/noop", "--out", str(tmp_path)]
    assert main(argv) == 2
    assert launched == []
    assert "--remote" in capsys.readouterr().err


def test_remote_rank0_writes_no_evidence(launched, rank, dns, tmp_path, monkeypatch):
    import ostia_dev.bench.ostia_bench as ob

    monkeypatch.setattr(ob.evidence, "snapshot", lambda: {"nic": {}, "nvlink": {}})
    rank(0)
    assert _ostia_run(tmp_path, "--remote", "--evidence") == 0
    assert not (tmp_path / "t").exists()


def test_a_second_run_with_the_same_run_id_appends(launched, tmp_path):
    assert _ostia_run(tmp_path) == 0
    assert _ostia_run(tmp_path) == 0
    lines = (tmp_path / "t" / "results.jsonl").read_text().splitlines()
    assert len(lines) == 2


def test_remote_ranks_meet_on_the_next_port_first(launched, monkeypatch, dns, tmp_path):
    import ostia_dev.bench.ostia_bench as ob

    met = []
    monkeypatch.setattr(ob, "_barrier", lambda r, host, port: met.append((r, host, port)))
    for k, v in {**PEER, "OSTIA_RANK": "1"}.items():
        monkeypatch.setenv(k, v)
    assert _ostia_run(tmp_path, "--remote") == 0
    assert met == [(1, "j-0.j", 29401)]


def test_barrier_between_two_real_drivers():
    import socket
    import threading

    import ostia_dev.bench.ostia_bench as ob

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    done = []
    t = threading.Thread(target=lambda: done.append(ob._barrier(0, "127.0.0.1", port)))
    t.start()
    ob._barrier(1, "127.0.0.1", port)
    t.join(10)
    assert done == [None] and not t.is_alive()


def test_barrier_times_out_with_a_clear_error(monkeypatch):
    import ostia_dev.bench.ostia_bench as ob

    monkeypatch.setattr(ob, "BARRIER_WAIT", 1)
    with pytest.raises(ob.BenchError) as e:
        ob._barrier(1, "127.0.0.1", 9)  # nothing listens on the discard port
    assert "rank 1 did not meet its peer" in str(e.value) and "RFC-0005 §4.11" in str(e.value)


TOPO = "topo1:sha256:" + "ab" * 32
SUMMARY = (
    "  gpus: 1 GPU(s)\n  error nvml-missing: NVML could not be loaded\n  status: partial, exit 2"
)


def _script(path, body):
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


def _tool(path, *, code=0, out=TOPO, err="", count=None):
    tally = f'echo call >> "{count}"\n' if count else ""
    return _script(
        path, f"{tally}printf '%s\\n' '{out}'\nprintf '%s\\n' '{err}' >&2\nexit {code}\n"
    )


DEVICES = '{"src_bus": "0000:3b:00.0", "dst_bus": "0000:86:00.0"}'


@pytest.fixture
def program(tmp_path):
    """An ostia-format program that prints one record with `devices` and its own env."""
    record = (
        '{"bench": "p2p_copy", "params": {"bytes": 1}, "unit": "GB/s", "higher_is_better": true,'
        f' "samples": [1.0, 2.0], "devices": {DEVICES}}}'
    )
    return _script(tmp_path / "p2p_copy", f"echo '{record}'\n")


def _drive(tmp_path, program, *flags):
    argv = ["run", "--format", "ostia", "--bench", str(program), "--run-id", "t"]
    return main([*argv, "--out", str(tmp_path / "results"), *flags])


def _records(tmp_path):
    lines = (tmp_path / "results" / "t" / "results.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


def test_the_topology_id_reaches_every_record_with_one_call(program, tmp_path, monkeypatch, capsys):
    count = tmp_path / "count"
    monkeypatch.setenv("OSTIA_TOPO_CAPTURE", str(_tool(tmp_path / "cap", count=count)))
    timeouts = []
    real = subprocess.run
    monkeypatch.setattr(
        ob.subprocess,
        "run",
        lambda cmd, **kw: timeouts.append(kw.get("timeout")) or real(cmd, **kw),
    )
    assert _drive(tmp_path, program, "--runs", "2") == 0
    [record] = _records(tmp_path)
    assert record["compat"]["topology"] == TOPO
    assert record["provenance"]["topology_source"] == "ostia-topo-capture --print-id"
    assert count.read_text().splitlines() == ["call"]
    assert 120 in timeouts
    assert "warning" not in capsys.readouterr().err
    validate(record)


def test_the_programs_devices_survive_into_the_record(program, tmp_path):
    assert _drive(tmp_path, program) == 0
    [record] = _records(tmp_path)
    assert record["devices"] == json.loads(DEVICES)
    validate(record)


def test_lookup_order_is_the_variable_then_the_build_dir_then_path(tmp_path, monkeypatch, program):
    ids = {n: "topo1:sha256:" + n * 64 for n in "123"}
    build = tmp_path / "build"
    (build / "fabric" / "tools" / "topo-capture").mkdir(parents=True)
    on_path = tmp_path / "bin"
    on_path.mkdir()
    _tool(on_path / "ostia-topo-capture", out=ids["3"])
    _tool(build / "fabric" / "tools" / "topo-capture" / "ostia-topo-capture", out=ids["2"])
    monkeypatch.setenv("PATH", f"{on_path}:/usr/bin:/bin")
    flags = ["--build-dir", str(build)]

    def topology():
        shutil.rmtree(tmp_path / "results", ignore_errors=True)
        assert _drive(tmp_path, program, *flags) == 0
        return _records(tmp_path)[0]["compat"]["topology"]

    assert topology() == ids["2"]
    monkeypatch.setenv("OSTIA_TOPO_CAPTURE", str(_tool(tmp_path / "env-tool", out=ids["1"])))
    assert topology() == ids["1"]
    monkeypatch.delenv("OSTIA_TOPO_CAPTURE")
    (build / "fabric" / "tools" / "topo-capture" / "ostia-topo-capture").unlink()
    assert topology() == ids["3"]


FAILURES = [
    ("absent", {}, "was not found"),
    ("partial", {"code": 2, "err": SUMMARY}, "a partial capture"),
    ("leak", {"code": 3, "err": SUMMARY}, "a failed leak check"),
    ("timeout", {"hang": True}, "did not finish"),
]


def _failing_tool(tmp_path, monkeypatch, spec):
    if spec.get("hang"):
        monkeypatch.setattr(ob, "CAPTURE_TIMEOUT", 1)
        tool = _script(tmp_path / "cap", "exec sleep 30\n")
    elif spec:
        tool = _tool(tmp_path / "cap", **spec)
    else:
        return
    monkeypatch.setenv("OSTIA_TOPO_CAPTURE", str(tool))


@pytest.mark.parametrize(("name", "spec", "outcome"), FAILURES, ids=[f[0] for f in FAILURES])
def test_a_missing_topology_warns_once_and_records_null(
    name, spec, outcome, program, tmp_path, monkeypatch, capsys
):
    _failing_tool(tmp_path, monkeypatch, spec)
    assert _drive(tmp_path, program) == 0
    [record] = _records(tmp_path)
    assert record["compat"]["topology"] is None
    assert "topology_source" not in record["provenance"]
    err = capsys.readouterr().err
    assert len([ln for ln in err.splitlines() if ln.startswith("warning:")]) == 1
    assert outcome in err
    if spec.get("err"):
        assert "  error nvml-missing: NVML could not be loaded" in err.splitlines()


@pytest.mark.parametrize(("name", "spec", "outcome"), FAILURES, ids=[f[0] for f in FAILURES])
def test_require_topology_turns_the_warning_into_a_contract_error(
    name, spec, outcome, program, tmp_path, monkeypatch, capsys
):
    _failing_tool(tmp_path, monkeypatch, spec)
    assert _drive(tmp_path, program, "--require-topology") == 1
    err = capsys.readouterr().err
    assert err.startswith("error:") and "warning:" not in err
    assert outcome in err and "rule:" in err and "fix:" in err and "see:" in err
    if spec.get("err"):
        assert "  error nvml-missing: NVML could not be loaded" in err.splitlines()
    assert not (tmp_path / "results").exists()


def test_a_non_executable_variable_is_not_found_even_with_a_tool_on_path(
    program, tmp_path, monkeypatch, capsys
):
    on_path = tmp_path / "bin"
    on_path.mkdir()
    _tool(on_path / "ostia-topo-capture")
    monkeypatch.setenv("PATH", f"{on_path}:/usr/bin:/bin")
    plain = tmp_path / "plain"
    plain.write_text("not a program")
    monkeypatch.setenv("OSTIA_TOPO_CAPTURE", str(plain))
    assert _drive(tmp_path, program) == 0
    assert _records(tmp_path)[0]["compat"]["topology"] is None
    assert "was not found" in capsys.readouterr().err


def test_remote_never_runs_the_capture_tool(launched, rank, dns, tmp_path, monkeypatch):
    monkeypatch.setattr(ob, "capture_topology", lambda build_dir: pytest.fail("looked up"))
    rank(1)
    assert _ostia_run(tmp_path, "--remote") == 0
    [record] = _records_at(tmp_path)
    assert record["compat"]["topology"] is None


def test_remote_cannot_require_a_topology(launched, rank, tmp_path, capsys):
    rank(1)
    assert _ostia_run(tmp_path, "--remote", "--require-topology") == 2
    assert launched == []


def _records_at(root):
    return [json.loads(line) for line in (root / "t" / "results.jsonl").read_text().splitlines()]


def test_convert_never_looks_up_a_topology(tmp_path, monkeypatch):
    monkeypatch.setattr(ob, "capture_topology", lambda build_dir: pytest.fail("looked up"))
    assert main(["convert", "--run-id", "c", "--out", str(tmp_path), str(FIXTURE)]) == 0


def test_programs_run_in_pci_bus_order(launched, tmp_path):
    assert _ostia_run(tmp_path) == 0
    [(_, env)] = launched
    assert env["CUDA_DEVICE_ORDER"] == "PCI_BUS_ID"


def test_nvbench_runs_in_pci_bus_order(tmp_path, monkeypatch):
    seen = tmp_path / "order"
    _script(
        tmp_path / "nv",
        f'echo "$CUDA_DEVICE_ORDER" > {seen}\ncp {FIXTURE} "$2"\n',
    )
    monkeypatch.setenv("CUDA_DEVICE_ORDER", "FASTEST_FIRST")
    assert main(["median-seconds", "--bench", str(tmp_path / "nv")]) == 0
    assert seen.read_text().strip() == "PCI_BUS_ID"
