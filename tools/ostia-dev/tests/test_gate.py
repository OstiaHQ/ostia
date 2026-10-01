"""`remote gate`: a setup's gate workloads on a k8s machine (RFC-0005 §6; partial, A3 Q10)."""

import json

import pytest
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
    """Stands in for run_k8s: builds the plan like core.prepare and writes evidence."""

    def __init__(self, repo, evidence=None, code=0):
        self.repo, self.evidence, self.code = repo, evidence, code
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
    assert names[-5:] == [
        "evidence-probe",
        "bench-p2p_copy",
        "bench-pipelining",
        "bench-batching",
        "bench-dual_link",
    ]
    assert "--probe nvlink" in cmds["evidence-probe"] and "--probe ib" not in cmds["evidence-probe"]
    assert "--remote" not in cmds["bench-p2p_copy"]
    assert "--mode nvlink" in cmds["bench-dual_link"]
    assert "ostia_fabric_bench_p2p_copy" in cmds["bench-p2p_copy"]
    assert "--run-id k8s-x-1" in cmds["bench-p2p_copy"] and "--evidence" in cmds["bench-p2p_copy"]
    assert run.spec.extra["pods"] == 1 and run.spec.profile == "a100x4"
    assert (run.spec.extra["context"], run.spec.extra["namespace"]) == ("c1", "ostia-gate")
    assert run.plan.suite == "gate"


def test_plan_rdma_pair_two_pods(tmp_path, cfg):
    run = FakeRun(tmp_path, code=1)
    _gate(tmp_path, cfg, _setup(tmp_path, RDMA, "rdma-pair", nodes=2), run)
    cmds = _commands(run.plan)
    assert "--probe ib" in cmds["evidence-probe"]
    assert "--remote" in cmds["bench-rdma_put"] and "--mem cuda" in cmds["bench-rdma_put"]
    assert "--mode rails" in cmds["bench-dual_link"] and "--remote" in cmds["bench-dual_link"]
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
    err = capsys.readouterr().err
    assert "RFC-0004 PR 7" in err and "RFC-0003 PR 6" in err


def test_baseline_runs_compare(tmp_path, cfg, monkeypatch):
    seen = {}

    def compare_main(argv):
        seen["argv"] = argv
        return 1

    monkeypatch.setattr(gate, "_compare_main", lambda: compare_main)
    monkeypatch.setattr(gate, "_evidence_problems", lambda workloads, ev_dir: [])
    base = tmp_path / "base.json"
    run = FakeRun(tmp_path)
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
