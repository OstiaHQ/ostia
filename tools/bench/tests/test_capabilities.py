from pathlib import Path

import pytest

from tools.bench.capabilities import SetupError, evaluate, load_setup, main

SETUPS = Path(__file__).resolve().parents[3] / "infra" / "setups"

SETUP = """\
schema: 1
name: example
gate: [p2p_copy, dual_link]
also_run: [topo_capture, onpath_placement]
max_duration_hours: 2
max_usd_per_hour: 8
machines:
  primary:
    backend: skypilot
    cloud: runpod
    purchase: on-demand
    nodes: 1
    accelerators: A100-80GB-SXM:4
    capabilities: [nvlink-p2p, cuda-ipc]
  fallback:
    backend: skypilot
    cloud: lambda
    purchase: on-demand
    nodes: 1
    accelerators: A100-40GB-SXM:1
    capabilities: [cuda-ipc]
"""


def test_every_committed_setup_supports_its_gate_on_both_machines():
    files = sorted(SETUPS.glob("*.yaml"))
    assert {f.stem for f in files} == {"nvlink-node", "rdma-pair", "tcp-efa-pair"}
    for f in files:
        setup = load_setup(f)
        for machine, results in evaluate(setup).items():
            missing = [r for r in results if r.required and not r.supported]
            assert not missing, (f.name, machine, missing)


def test_gate_workloads_match_rfc_0001():
    gates = {f.stem: load_setup(f)["gate"] for f in SETUPS.glob("*.yaml")}
    assert gates == {
        "nvlink-node": ["p2p_copy", "pipelining", "batching", "dual_link"],
        "rdma-pair": ["rdma_put", "gdr_stream", "dual_link"],
        "tcp-efa-pair": [],
    }


def test_a_missing_gate_capability_fails_and_also_run_is_unsupported(tmp_path, capsys):
    path = tmp_path / "example.yaml"
    path.write_text(SETUP)
    results = {r.workload: r for r in evaluate(load_setup(path))["fallback"]}
    assert not results["p2p_copy"].supported and results["p2p_copy"].required
    assert "nvlink-p2p" in results["p2p_copy"].reason
    assert not results["dual_link"].supported  # one GPU: no two NVLink paths
    assert not results["onpath_placement"].supported and not results["onpath_placement"].required
    assert main(["--setups", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "error: example/fallback cannot run gate workload p2p_copy" in out
    assert "unsupported" in out


def test_dual_link_across_nodes_needs_two_rails(tmp_path):
    text = SETUP.replace("nodes: 1", "nodes: 2", 1).replace(
        "capabilities: [nvlink-p2p, cuda-ipc]", "capabilities: [nvlink-p2p, gpudirect-rdma]", 1
    )
    path = tmp_path / "example.yaml"
    path.write_text(text)
    results = {r.workload: r for r in evaluate(load_setup(path))["primary"]}
    assert not results["dual_link"].supported and "multi-rail" in results["dual_link"].reason


def test_schema_errors(tmp_path):
    path = tmp_path / "example.yaml"
    path.write_text(SETUP.replace("gate: [p2p_copy, dual_link]", "gate: [p2p_kopy]"))
    with pytest.raises(SetupError, match="unknown workload 'p2p_kopy'"):
        load_setup(path)
    path.write_text(SETUP.replace("schema: 1", "schema: 2"))
    with pytest.raises(SetupError, match="schema"):
        load_setup(path)
    path.write_text(SETUP.replace("name: example", "name: other"))
    with pytest.raises(SetupError, match="file name"):
        load_setup(path)


# Today's results for the committed setups; adding the k8s machine type must not move them.
SNAPSHOT = [
    ("nvlink-node", "fallback", "batching", True, True, ""),
    ("nvlink-node", "fallback", "dual_link", True, True, ""),
    ("nvlink-node", "fallback", "onpath_placement", False, True, ""),
    ("nvlink-node", "fallback", "p2p_copy", True, True, ""),
    ("nvlink-node", "fallback", "pipelining", True, True, ""),
    ("nvlink-node", "fallback", "topo_capture", False, True, ""),
    ("nvlink-node", "primary", "batching", True, True, ""),
    ("nvlink-node", "primary", "dual_link", True, True, ""),
    ("nvlink-node", "primary", "onpath_placement", False, True, ""),
    ("nvlink-node", "primary", "p2p_copy", True, True, ""),
    ("nvlink-node", "primary", "pipelining", True, True, ""),
    ("nvlink-node", "primary", "topo_capture", False, True, ""),
    ("rdma-pair", "fallback", "dual_link", True, True, ""),
    ("rdma-pair", "fallback", "gdr_stream", True, True, ""),
    ("rdma-pair", "fallback", "rdma_put", True, True, ""),
    ("rdma-pair", "fallback", "topo_capture", False, True, ""),
    ("rdma-pair", "primary", "dual_link", True, True, ""),
    ("rdma-pair", "primary", "gdr_stream", True, True, ""),
    ("rdma-pair", "primary", "rdma_put", True, True, ""),
    ("rdma-pair", "primary", "topo_capture", False, True, ""),
    ("tcp-efa-pair", "fallback", "tcp_put", False, True, ""),
    ("tcp-efa-pair", "fallback", "topo_capture", False, True, ""),
    ("tcp-efa-pair", "primary", "tcp_put", False, True, ""),
    ("tcp-efa-pair", "primary", "topo_capture", False, True, ""),
]


def test_evaluate_snapshot_of_committed_setups():
    rows = [
        (path.stem, machine, r.workload, r.required, r.supported, r.reason)
        for path in sorted(SETUPS.glob("*.yaml"))
        for machine, results in evaluate(load_setup(path)).items()
        for r in results
    ]
    assert sorted(rows) == SNAPSHOT


K8S_FALLBACK = """\
  fallback:
    backend: k8s
    machine: rdma-a100x2
    pods: 2
    accelerators: A100-80GB-SXM:1
    capabilities: [gpudirect-rdma, multi-rail, tcp]
"""


def _with_k8s_fallback(tmp_path, fallback=K8S_FALLBACK, gate="[rdma_put, tcp_put]"):
    primary = SETUP.split("  fallback:\n")[0].replace(
        "gate: [p2p_copy, dual_link]", f"gate: {gate}"
    )
    path = tmp_path / "example.yaml"
    path.write_text(primary + fallback)
    return path


def test_unknown_backend(tmp_path):
    path = tmp_path / "example.yaml"
    path.write_text(
        SETUP.replace("backend: skypilot\n    cloud: lambda", "backend: slurm\n    cloud: lambda")
    )
    with pytest.raises(SetupError, match="backend 'slurm'.*skypilot, azure, k8s"):
        load_setup(path)


@pytest.mark.parametrize("drop", ["machine", "pods", "accelerators", "capabilities"])
def test_k8s_machine_requires_its_fields(tmp_path, drop):
    fallback = "".join(
        line + "\n" for line in K8S_FALLBACK.splitlines() if not line.strip().startswith(drop + ":")
    )
    with pytest.raises(SetupError, match=f"missing {drop}"):
        load_setup(_with_k8s_fallback(tmp_path, fallback))


@pytest.mark.parametrize("extra", ["purchase: on-demand", "cloud: aws", "nodes: 2"])
def test_k8s_machine_rejects_rented_fields(tmp_path, extra):
    with pytest.raises(SetupError, match=extra.split(":")[0]):
        load_setup(_with_k8s_fallback(tmp_path, K8S_FALLBACK + f"    {extra}\n"))


def test_k8s_machine_pods_is_one_or_two(tmp_path):
    with pytest.raises(SetupError, match="pods must be 1 or 2"):
        load_setup(_with_k8s_fallback(tmp_path, K8S_FALLBACK.replace("pods: 2", "pods: 3")))


def test_k8s_machine_nodes_from_pods(tmp_path):
    setup = load_setup(_with_k8s_fallback(tmp_path))
    assert setup["machines"]["fallback"]["nodes"] == 2
    fallback = {r.workload: r for r in evaluate(setup)["fallback"]}
    assert fallback["rdma_put"].supported and fallback["tcp_put"].supported
