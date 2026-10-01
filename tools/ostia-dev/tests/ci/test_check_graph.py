"""Tests for ostia_dev/ci/check_graph.py (RFC-0001 §3.3, enforcement part 2)."""

from pathlib import Path

from ostia_dev.ci.check_graph import main, violations

GOLDEN = Path(__file__).resolve().parents[4] / "tests" / "cmake" / "layering" / "golden.dot"
UPWARD = "ostia_fabric -> ostia_exchange"


def test_golden_upward_genex_edge_fails(capsys):
    assert main(["--dot", str(GOLDEN)]) == 1
    out = capsys.readouterr().out
    assert "error: ostia_fabric links ostia_exchange" in out
    assert "fabric may depend on: telemetry" in out
    assert "RFC-0001 §3.3" in out


def test_allowed_edges_pass(tmp_path):
    dot = tmp_path / "ok.dot"
    lines = [line for line in GOLDEN.read_text().splitlines() if UPWARD not in line]
    dot.write_text("\n".join(lines) + "\n")
    assert main(["--dot", str(dot)]) == 0


def test_external_nodes_are_ignored():
    text = """
    "node0" [ label = "ostia_fabric\\n(ostia::fabric)", shape = doubleoctagon ];
    "node1" [ label = "GTest::gtest", shape = octagon ];
    "node0" -> "node1"  // ostia_fabric -> GTest::gtest
    """
    assert violations(text) == []


def test_alias_only_label_maps_to_component():
    text = """
    "node0" [ label = "ostia::telemetry", shape = doubleoctagon ];
    "node1" [ label = "ostia::fabric", shape = doubleoctagon ];
    "node0" -> "node1"
    """
    [v] = violations(text)
    assert (v.source, v.target) == ("telemetry", "fabric")
