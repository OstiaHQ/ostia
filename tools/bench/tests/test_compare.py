"""Tests for tools/bench/compare.py (RFC-0001 §6.3)."""

import copy
import json
import random

import pytest

from tools.bench.compare import compare_case, compare_runs, main, pool_baseline
from tools.bench.tests.test_schema import RECORD


def noisy(mean, n=20, sd=0.2, seed=1):
    rng = random.Random(seed)
    return [rng.gauss(mean, sd) for _ in range(n)]


def rec(samples, sha="3f2a9c1", run="r1", bench="p2p_copy", **compat):
    r = copy.deepcopy(RECORD)
    r["samples"] = samples
    r["bench"] = bench
    r["provenance"]["git_sha"] = sha
    r["provenance"]["run_id"] = run
    r["compat"].update(compat)
    return r


def test_same_performance_passes():
    assert compare_case(noisy(44, seed=1), noisy(44, seed=2), True).outcome == "pass"


def test_ten_percent_slower_is_a_regression():
    assert compare_case(noisy(44, seed=1), noisy(39.6, seed=2), True).outcome == "regression"


def test_lower_is_better_metrics_flip_the_direction():
    base, cand = noisy(10, seed=1), noisy(11, seed=2)  # 10% more latency
    assert compare_case(base, cand, higher_is_better=False).outcome == "regression"
    assert compare_case(cand, base, higher_is_better=False).outcome == "pass"


def test_near_the_threshold_is_inconclusive():
    base = noisy(100, sd=4, seed=1)
    cand = noisy(95, sd=4, seed=2)
    assert compare_case(base, cand, True).outcome == "inconclusive"


def test_fewer_than_ten_samples_is_inconclusive():
    assert compare_case(noisy(44, n=9), noisy(44, n=20), True).outcome == "inconclusive"


def test_the_interval_is_deterministic():
    a = compare_case(noisy(44, seed=1), noisy(43, seed=2), True)
    b = compare_case(noisy(44, seed=1), noisy(43, seed=2), True)
    assert (a.lo, a.hi) == (b.lo, b.hi)


def test_different_commits_on_a_compatible_box_compare():
    [r] = compare_runs(
        [rec(noisy(44, seed=1), sha="aaaaaaa")], [rec(noisy(44, seed=2), sha="bbbbbbb")]
    )
    assert r.outcome == "pass"


def test_incompatible_boxes_are_skipped_not_compared():
    [r] = compare_runs([rec(noisy(44))], [rec(noisy(44), gpu="H100-SXM5-80GB")])
    assert r.outcome == "skipped" and "gpu" in r.detail


def test_a_case_missing_from_the_manifest_is_invalid():
    manifest = [
        {"bench": "p2p_copy", "params": RECORD["params"]},
        {"bench": "pipelining", "params": {"chunk": 4}},
    ]
    results = compare_runs([rec(noisy(44))], [rec(noisy(44))], manifest)
    assert [r.outcome for r in results if r.bench == "pipelining"] == ["invalid"]


def test_a_duplicate_case_is_invalid():
    results = compare_runs([rec(noisy(44))], [rec(noisy(44)), rec(noisy(44), run="r2")])
    assert [r.outcome for r in results] == ["invalid"]
    assert "duplicate" in results[0].detail


def write(path, records):
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


def test_cli_invalid_inputs(tmp_path, capsys):
    base = write(tmp_path / "base.jsonl", [rec(noisy(44))])
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    malformed = tmp_path / "bad.jsonl"
    malformed.write_text("{not json\n")
    nan = write(tmp_path / "nan.jsonl", [rec([1.0] * 9 + [float("nan")])])
    missing = tmp_path / "missing.jsonl"
    for cand in (empty, malformed, nan, missing):
        assert main(["--baseline", str(base), "--candidate", str(cand)]) == 1
        assert "invalid" in capsys.readouterr().out


def test_cli_required_gate_fails_on_inconclusive(tmp_path, capsys):
    base = write(tmp_path / "b.jsonl", [rec(noisy(100, sd=4, seed=1))])
    cand = write(tmp_path / "c.jsonl", [rec(noisy(95, sd=4, seed=2))])
    assert main(["--baseline", str(base), "--candidate", str(cand)]) == 0
    assert main(["--baseline", str(base), "--candidate", str(cand), "--require-pass"]) == 1
    assert "inconclusive" in capsys.readouterr().out


def test_write_baseline_pools_machines(tmp_path):
    m1 = write(tmp_path / "m1.jsonl", [rec(noisy(44, n=10, seed=1), run="m1")])
    m2 = write(tmp_path / "m2.jsonl", [rec(noisy(44, n=10, seed=2), run="m2")])
    out = tmp_path / "nvlink-node.json"
    assert main(["--write-baseline", str(out), "--setup", "nvlink-node", str(m1), str(m2)]) == 0
    baseline = json.loads(out.read_text())
    [r] = baseline["records"]
    assert len(r["samples"]) == 20 and r["machines"] == 2
    assert baseline["setup"] == "nvlink-node"


def test_pooling_refuses_incompatible_machines():
    with pytest.raises(ValueError, match="incompatible"):
        pool_baseline([[rec(noisy(44))], [rec(noisy(44), driver="570.1")]], "x")
