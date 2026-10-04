"""`remote gate`: a setup's gate workloads on a k8s machine (RFC-0005 §6; partial, A3 Q10)."""

import json

import pytest
from fakes.capture import capture_files
from ostia_dev import config
from ostia_dev.errors import UsageError
from ostia_dev.remote.k8s import gate

CONFIG = """schema = 1
[remote.k8s.contexts.c1]
provider = "gke"
namespace = "ostia-gate"
[remote.k8s.machines.nvlink-a100x4]
context = "c1"
namespace = "ostia-gate"
profile = "a100x4"
[remote.k8s.machines.rdma-a100x2]
context = "c1"
namespace = "ostia-rdma"
profile = "ib"
[remote.k8s.profiles.a100x4]
kind = "gpu"
gpus = 4
compute_capability = "8.0"
cpu = "16"
memory = "64Gi"
ephemeral_storage = "100Gi"
[remote.k8s.profiles.a100x4.gke]
node_selector = { "cloud.google.com/gke-accelerator" = "nvidia-a100-80gb" }
[remote.k8s.profiles.ib]
kind = "rdma"
gpus = 1
compute_capability = "8.0"
cpu = "16"
memory = "64Gi"
ephemeral_storage = "100Gi"
rdma_resources = { "rdma/rdma_shared_device_a" = "1" }
rdma_nics = "mlx5_0:1,mlx5_1:1"
[remote.k8s.profiles.ib.gke]
node_selector = { "example.com/ib" = "true" }
"""

PRIMARY = """\
  primary:
    backend: skypilot
    cloud: runpod
    purchase: on-demand
    nodes: {nodes}
    accelerators: A100-80GB-SXM:4
    capabilities: [nvlink-p2p, cuda-ipc, gpudirect-rdma, multi-rail]
"""
NVLINK = """\
schema: 1
name: nvlink-node
gate: [p2p_copy, pipelining, batching, dual_link]
also_run: [topo_capture]
max_duration_hours: 4
max_usd_per_hour: 8
machines:
{primary}  fallback:
    backend: k8s
    machine: nvlink-a100x4
    pods: 1
    accelerators: A100-80GB-SXM:4
    capabilities: [nvlink-p2p, cuda-ipc]
"""
RDMA = """\
schema: 1
name: rdma-pair
gate: [rdma_put, gdr_stream, dual_link]
also_run: [topo_capture]
max_duration_hours: 4
max_usd_per_hour: 8
machines:
{primary}  fallback:
    backend: k8s
    machine: rdma-a100x2
    pods: 2
    accelerators: A100-80GB-SXM:1
    capabilities: [gpudirect-rdma, multi-rail]
"""
EVIDENCE_OK = {
    "p2p_copy": {"lanes": [], "program": {"peer_access": "1", "moved": "100"}},
}


@pytest.fixture
def cfg(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(CONFIG)
    return config.load(path)


def _setup(tmp_path, text, name, nodes=1):
    path = tmp_path / f"{name}.yaml"
    path.write_text(text.format(primary=PRIMARY.format(nodes=nodes)))
    return path


class FakeRun:
    """Stands in for run_k8s: builds the plan like core.prepare and writes evidence and
    the records of `records` (default: one per workload with evidence), or `full` schema-1
    records; `captures` ({node: status entry}) writes capture/ as K8sBackend.collect would."""

    def __init__(self, repo, evidence=None, code=0, records=None, full=None, captures=None):
        self.repo, self.evidence, self.code, self.records = repo, evidence, code, records
        self.full, self.captures = full, captures
        self.spec = self.plan = None

    def __call__(self, spec, cfg, repo):
        from ostia_dev.remote.profiles import resolve

        self.spec = spec
        profile = resolve(spec.profile, "gke", cfg)
        self.plan = spec.extra["plan"](profile, spec.envs[0], "k8s-x-1")
        ev = repo / "bench" / "results" / "k8s-x-1" / "evidence"
        ev.mkdir(parents=True, exist_ok=True)
        for w, doc in (self.evidence or {}).items():
            (ev / f"{w}.json").write_text(json.dumps({"schema": 1, "workload": w, **doc}))
        names = self.records if self.records is not None else list(self.evidence or {})
        docs = self.full if self.full is not None else [{"bench": w, "params": {}} for w in names]
        (ev.parent / "results.jsonl").write_text("".join(json.dumps(d) + "\n" for d in docs))
        if self.captures is not None:
            root = spec.results / "k8s-x-1" / "capture"
            for node, entry in self.captures.items():
                (root / node).mkdir(parents=True)
                files = capture_files(entry["status"] or "complete")
                kept = files if entry["result"] == "accepted" else {"diagnostics.txt": b"x\n"}
                for name, data in kept.items():
                    (root / node / name).write_bytes(data)
            (root / "status.json").write_text(json.dumps(self.captures))
        return self.code


def _gate(tmp_path, cfg, path, run, **kw):
    kw.setdefault("fallback", True)
    return gate.gate(path, cfg=cfg, repo=tmp_path, run=run, results=tmp_path / "res", **kw)


def test_failure_unmapped_machine(tmp_path, cfg):
    path = _setup(
        tmp_path, NVLINK.replace("machine: nvlink-a100x4", "machine: other"), "nvlink-node"
    )
    with pytest.raises(UsageError) as e:
        _gate(tmp_path, cfg, path, FakeRun(tmp_path))
    assert "[remote.k8s.machines.other]" in e.value.message and e.value.code == 2


def test_failure_not_a_k8s_machine(tmp_path, cfg):
    path = _setup(tmp_path, NVLINK, "nvlink-node")
    with pytest.raises(UsageError) as e:
        _gate(tmp_path, cfg, path, FakeRun(tmp_path), fallback=False)
    assert "ostia-dev rent" in e.value.message and "RFC-0004" in e.value.message


def test_failure_bad_setup_file_is_exit_2(tmp_path, cfg):
    path = _setup(tmp_path, NVLINK.replace("backend: k8s", "backend: slurm"), "nvlink-node")
    with pytest.raises(UsageError) as e:
        _gate(tmp_path, cfg, path, FakeRun(tmp_path))
    assert "slurm" in e.value.message


def test_failure_profile_gpus_below_accelerators(tmp_path, cfg):
    path = _setup(
        tmp_path,
        NVLINK.replace(
            "A100-80GB-SXM:4\n    capabilities: [nvlink-p2p, cuda-ipc]\n",
            "A100-80GB-SXM:8\n    capabilities: [nvlink-p2p, cuda-ipc]\n",
        ),
        "nvlink-node",
    )
    with pytest.raises(UsageError) as e:
        _gate(tmp_path, cfg, path, FakeRun(tmp_path))
    assert "a100x4" in e.value.message and "8" in e.value.message


def test_failure_rdma_workload_on_gpu_profile(tmp_path, cfg):
    path = _setup(
        tmp_path,
        RDMA.replace("machine: rdma-a100x2", "machine: nvlink-a100x4"),
        "rdma-pair",
        nodes=2,
    )
    run = FakeRun(tmp_path)
    with pytest.raises(UsageError) as e:
        _gate(tmp_path, cfg, path, run)
    msg = e.value.message
    assert "rdma_put" in msg and "a100x4" in msg and "§4.12" in msg
    assert run.spec is None


def test_failure_gate_capability_missing(tmp_path, cfg, capsys):
    path = _setup(
        tmp_path,
        NVLINK.replace("capabilities: [nvlink-p2p, cuda-ipc]\n", "capabilities: [cuda-ipc]\n"),
        "nvlink-node",
    )
    run = FakeRun(tmp_path)
    assert _gate(tmp_path, cfg, path, run) == 1
    assert run.spec is None and "cannot run gate workload p2p_copy" in capsys.readouterr().out


def _commands(plan):
    return {s.name: " ".join(s.argv) for s in plan.steps}


def test_plan_nvlink_node(tmp_path, cfg):
    run = FakeRun(tmp_path, code=1)
    _gate(tmp_path, cfg, _setup(tmp_path, NVLINK, "nvlink-node"), run)
    cmds = _commands(run.plan)
    names = [s.name for s in run.plan.steps]
    assert names[-6:] == [
        "evidence-probe",
        "bench-p2p_copy",
        "bench-pipelining",
        "bench-batching",
        "bench-dual_link",
        "capture",
    ]
    assert "--probe nvlink" in cmds["evidence-probe"] and "--probe ib" not in cmds["evidence-probe"]
    assert "--remote" not in cmds["bench-p2p_copy"]
    assert "--mode nvlink" in cmds["bench-dual_link"]
    assert "ostia_fabric_bench_p2p_copy" in cmds["bench-p2p_copy"]
    assert "--run-id k8s-x-1" in cmds["bench-p2p_copy"] and "--evidence" in cmds["bench-p2p_copy"]
    assert run.spec.extra["pods"] == 1 and run.spec.profile == "a100x4"
    assert (run.spec.extra["context"], run.spec.extra["namespace"]) == ("c1", "ostia-gate")
    assert run.plan.suite == "gate"


def test_plan_runs_the_bench_tools_through_ostia_dev(tmp_path, cfg):
    run = FakeRun(tmp_path, code=1)
    _gate(tmp_path, cfg, _setup(tmp_path, NVLINK, "nvlink-node"), run)
    cmds = _commands(run.plan)
    assert "ostia-dev bench evidence --probe nvlink" in cmds["evidence-probe"]
    assert "ostia-dev bench run --format ostia --evidence" in cmds["bench-p2p_copy"]
    assert "tools/bench" not in " ".join(cmds.values())


def test_plan_rdma_pair_two_pods(tmp_path, cfg):
    run = FakeRun(tmp_path, code=1)
    _gate(tmp_path, cfg, _setup(tmp_path, RDMA, "rdma-pair", nodes=2), run)
    cmds = _commands(run.plan)
    assert "--probe ib" in cmds["evidence-probe"]
    assert "--remote" in cmds["bench-rdma_put"] and "--mem cuda" in cmds["bench-rdma_put"]
    assert "--mode rails" in cmds["bench-dual_link"] and "--remote" in cmds["bench-dual_link"]
    assert "--nics mlx5_0:1,mlx5_1:1" in cmds["bench-dual_link"]
    assert run.spec.extra["pods"] == 2


def test_a_failed_run_returns_its_code(tmp_path, cfg):
    assert (
        _gate(tmp_path, cfg, _setup(tmp_path, NVLINK, "nvlink-node"), FakeRun(tmp_path, code=3))
        == 3
    )


def test_evidence_checked_on_the_host(tmp_path, cfg, capsys):
    bad = {
        "lanes": [{"tl": "tcp", "device": "eth0"}],
        "program": {"memory_type": "cuda", "moved": "100"},
        "nic_bytes": {},
        "nvlink_bytes": {},
    }
    run = FakeRun(tmp_path, evidence={w: bad for w in ("rdma_put", "gdr_stream", "dual_link")})
    assert _gate(tmp_path, cfg, _setup(tmp_path, RDMA, "rdma-pair", nodes=2), run) == 1
    out = capsys.readouterr().out
    assert "rdma_put" in out and "does not show the transport" in out


def test_missing_evidence_fails(tmp_path, cfg, capsys):
    run = FakeRun(tmp_path, evidence={})
    assert _gate(tmp_path, cfg, _setup(tmp_path, NVLINK, "nvlink-node"), run) == 1
    assert "no evidence for p2p_copy" in capsys.readouterr().out


def test_pending_checks_are_printed(tmp_path, cfg, capsys):
    _gate(tmp_path, cfg, _setup(tmp_path, NVLINK, "nvlink-node"), FakeRun(tmp_path, code=1))
    pending = [line for line in capsys.readouterr().err.splitlines() if "not checked yet" in line]
    assert len(pending) == 2 and all("RFC-0004 PR 7" in line for line in pending)
    assert not any("RFC-0003" in line for line in pending)


def test_plan_captures_after_the_bench_steps_as_a_report(tmp_path, cfg):
    run = FakeRun(tmp_path, code=1)
    _gate(tmp_path, cfg, _setup(tmp_path, NVLINK, "nvlink-node"), run)
    last = run.plan.steps[-1]
    assert (last.name, last.kind) == ("capture", "report")
    assert last.argv == ("pixi", "run", "--frozen", "-e", "cuda-12", "ostia-dev", "topo",
                         "capture", "--build-dir", "/w/build/cuda-12/release",
                         "--out", "/w/capture")  # fmt: skip


def test_no_capture_step_without_topo_capture(tmp_path, cfg):
    run = FakeRun(tmp_path, code=1)
    path = _setup(tmp_path, NVLINK.replace("[topo_capture]", "[]"), "nvlink-node")
    _gate(tmp_path, cfg, path, run)
    assert "capture" not in [s.name for s in run.plan.steps]


PAIR = "topo1:sha256:" + "cd" * 32
RDMA_RECORDS = [("rdma_put", {"bytes": 1}), ("gdr_stream", {"bytes": 1}),
                ("dual_link", {"path": "a", "mode": "rails"}),
                ("dual_link", {"path": "b", "mode": "rails"}),
                ("dual_link", {"path": "both", "mode": "rails"})]  # fmt: skip
OK = {"result": "accepted", "reason": None, "status": "complete", "fix": None}
COMPAT = {"gpu": "A100", "driver": "580.95", "cuda": "12.9", "nic": "none", "build_level": "off",
          "compiler": "gcc", "deps": "x"}  # fmt: skip


def _record(bench, params, topology=None, median=20.0):
    compat = dict(COMPAT, topology=topology)
    provenance = {"git_sha": "3f2a9c1", "date": "2026-10-04T00:00:00Z", "run_id": "k8s-x-1"}
    return {
        "schema": 1,
        "provenance": provenance,
        "compat": compat,
        "bench": bench,
        "params": params,
        "unit": "GB/s",
        "higher_is_better": True,
        "samples": [median] * 10,
        "median": median,
    }


@pytest.fixture
def pair_gate(tmp_path, cfg, monkeypatch):
    """A two-pod rdma-pair gate with full records; compare records what it was given."""
    from ostia_dev.bench import evidence

    seen = {}

    def compare_main(argv):
        text = (tmp_path / argv[argv.index("--candidate") + 1]).read_text()
        seen["records"] = [json.loads(line) for line in text.splitlines()]
        return 0

    def pair_id(d):
        seen["pair_json"] = json.loads((d / "pair.json").read_text())
        return PAIR

    monkeypatch.setattr(gate, "_compare_main", lambda: compare_main)
    monkeypatch.setattr(gate, "_pair_id", pair_id)
    monkeypatch.setattr(evidence, "check", lambda ev: [])
    workloads = ("rdma_put", "gdr_stream", "dual_link")

    def go(captures, **kw):
        full = [_record(b, p, median=20.0 + i) for i, (b, p) in enumerate(RDMA_RECORDS)]
        run = FakeRun(tmp_path, evidence={w: {} for w in workloads}, full=full, captures=captures)
        path = _setup(tmp_path, RDMA, "rdma-pair", nodes=2)
        kw.setdefault("baseline", tmp_path / "base.json")
        return _gate(tmp_path, cfg, path, run, **kw)

    return go, seen


def test_two_accepted_captures_write_pair_json_and_stamp_before_compare(pair_gate, tmp_path):
    go, seen = pair_gate
    assert go({"node-0": OK, "node-1": OK}) == 0
    assert seen["pair_json"] == {
        "schema": 1,
        "nodes": ["node-0", "node-1"],
        "link_class": "infiniband",
        "rails": [{"node-0": {"nic_index": 0}, "node-1": {"nic_index": 0}},
                  {"node-0": {"nic_index": 1}, "node-1": {"nic_index": 1}}],
        "measured": [{"rail": 0, "direction": "1->0", "bw_mbps": 22000, "test": "dual_link"},
                     {"rail": 1, "direction": "1->0", "bw_mbps": 23000, "test": "dual_link"}],
    }  # fmt: skip
    assert (tmp_path / "res" / "k8s-x-1" / "capture" / "pair.json").exists()
    assert len(seen["records"]) == len(RDMA_RECORDS)
    for r in seen["records"]:
        assert r["compat"]["topology"] == PAIR and r["provenance"]["topology_source"] == "gate"


REJECTED = {"result": "rejected", "reason": "the tar stream repeats a member", "status": None,
            "fix": "rerun"}  # fmt: skip
PARTIAL = {**OK, "status": "partial", "fix": "nvml.json missing (nvml_init)"}


@pytest.mark.parametrize(
    ("node1", "words"), [(REJECTED, "node-1 rejected"), (PARTIAL, "node-1 accepted (partial)")]
)
def test_an_unusable_capture_leaves_the_records_unstamped(
    pair_gate, tmp_path, capsys, node1, words
):
    go, seen = pair_gate
    assert go({"node-0": OK, "node-1": node1}, baseline=None) == 0
    assert "pair_json" not in seen
    assert not (tmp_path / "res" / "k8s-x-1" / "capture" / "pair.json").exists()
    text = (tmp_path / "bench" / "results" / "k8s-x-1" / "results.jsonl").read_text()
    assert "topology_source" not in text
    err = capsys.readouterr().err
    assert "warning: the records keep topology null" in err and words in err


def test_a_missing_status_file_leaves_the_records_unstamped(pair_gate, capsys):
    go, seen = pair_gate
    assert go(None, baseline=None) == 0
    assert "node-0 absent, node-1 absent" in capsys.readouterr().err


def test_a_rejected_capture_against_a_paired_baseline_fails_the_gate(
    pair_gate, tmp_path, monkeypatch, capsys
):
    from ostia_dev.bench import compare

    go, _ = pair_gate
    monkeypatch.setattr(gate, "_compare_main", lambda: compare.main)
    base = tmp_path / "base.json"
    records = [_record(b, p, topology=PAIR) for b, p in RDMA_RECORDS]
    base.write_text(json.dumps({"schema": 1, "setup": "rdma-pair", "records": records}))
    assert go({"node-0": OK, "node-1": REJECTED}, baseline=base) == 1
    out = capsys.readouterr().out
    assert "topology is cdcdcdcdcdcd in the baseline and null in the candidate" in out
    assert "error: the baseline has a pair id and these records have none" in out


def test_a_one_pod_gate_never_stamps(tmp_path, cfg, monkeypatch):
    monkeypatch.setattr(gate, "_pair_id", lambda d: pytest.fail("one pod has no pair"))
    monkeypatch.setattr(gate, "_evidence_problems", lambda workloads, ev_dir: [])
    workloads = ["p2p_copy", "pipelining", "batching", "dual_link"]
    run = FakeRun(tmp_path, records=workloads, captures={"node-0": OK})
    assert _gate(tmp_path, cfg, _setup(tmp_path, NVLINK, "nvlink-node"), run) == 0


def test_baseline_runs_compare(tmp_path, cfg, monkeypatch):
    seen = {}

    def compare_main(argv):
        seen["argv"] = argv
        return 1

    monkeypatch.setattr(gate, "_compare_main", lambda: compare_main)
    monkeypatch.setattr(gate, "_evidence_problems", lambda workloads, ev_dir: [])
    base = tmp_path / "base.json"
    run = FakeRun(tmp_path, records=["p2p_copy", "pipelining", "batching", "dual_link"])
    assert _gate(tmp_path, cfg, _setup(tmp_path, NVLINK, "nvlink-node"), run, baseline=base) == 1
    argv = seen["argv"]
    assert argv[argv.index("--baseline") + 1] == str(base)
    assert "--require-pass" in argv and "--evidence-dir" in argv


def test_fallback_flag_picks_fallback(tmp_path, cfg):
    text = NVLINK.format(primary=PRIMARY.format(nodes=1))
    swapped = text.replace("  fallback:", "  other:").replace("  primary:", "  fallback:")
    path = tmp_path / "nvlink-node.yaml"
    path.write_text(swapped.replace("  other:", "  primary:"))
    run = FakeRun(tmp_path, code=1)
    assert _gate(tmp_path, cfg, path, run, fallback=False) == 1
    assert run.spec.profile == "a100x4"


def test_missing_records_fail(tmp_path, cfg, capsys, monkeypatch):
    monkeypatch.setattr(gate, "_evidence_problems", lambda workloads, ev_dir: [])
    run = FakeRun(tmp_path, records=["p2p_copy", "pipelining", "batching"])
    assert _gate(tmp_path, cfg, _setup(tmp_path, NVLINK, "nvlink-node"), run) == 1
    assert "no benchmark records for dual_link" in capsys.readouterr().out


def test_failure_rails_without_rdma_nics(tmp_path, cfg):
    path = tmp_path / "config.toml"
    path.write_text(CONFIG.replace('rdma_nics = "mlx5_0:1,mlx5_1:1"\n', ""))
    with pytest.raises(UsageError) as e:
        _gate(
            tmp_path,
            config.load(path),
            _setup(tmp_path, RDMA, "rdma-pair", nodes=2),
            FakeRun(tmp_path),
        )
    assert "rdma_nics" in e.value.message and "[remote.k8s.profiles.ib]" in e.value.message
