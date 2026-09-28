"""Tests for tools/bench/schema.py (RFC-0001 §6.2)."""

import copy

import pytest

from tools.bench.schema import SchemaError, compat_key, validate

RECORD = {
    "schema": 1,
    "provenance": {"git_sha": "3f2a9c1", "date": "2026-11-02T10:14:00Z", "run_id": "nvlink-01"},
    "compat": {
        "gpu": "A100-SXM4-80GB",
        "driver": "580.95",
        "cuda": "12.8",
        "nic": "none",
        "topology": "sha256:5d1e",
        "build_level": "off",
        "compiler": "gcc-14 -O3",
        "deps": "pixi.lock:9ab3",
    },
    "bench": "p2p_copy",
    "params": {"bytes": 1073741824, "direction": "0->1", "concurrency": 1},
    "unit": "GB/s",
    "higher_is_better": True,
    "samples": [44.1, 44.3, 44.2, 44.2, 44.0, 44.3, 44.1, 44.2, 44.4, 44.2],
    "median": 44.2,
    "p5": 44.05,
    "p95": 44.35,
}


def test_valid_record():
    validate(RECORD)


def test_unknown_schema_version_is_rejected_with_an_explanation():
    r = dict(RECORD, schema=2)
    with pytest.raises(SchemaError, match="schema version 2 is not supported .*supported: 1"):
        validate(r)


@pytest.mark.parametrize("field", ["bench", "samples", "compat", "provenance", "unit"])
def test_missing_fields_are_rejected(field):
    r = copy.deepcopy(RECORD)
    del r[field]
    with pytest.raises(SchemaError, match=field):
        validate(r)


def test_non_finite_samples_are_rejected():
    r = dict(RECORD, samples=[1.0, float("nan")])
    with pytest.raises(SchemaError, match="non-finite"):
        validate(r)


def test_provenance_does_not_affect_compatibility():
    other = copy.deepcopy(RECORD)
    other["provenance"] = {"git_sha": "0000000", "date": "2027-01-01T00:00:00Z", "run_id": "x"}
    assert compat_key(other) == compat_key(RECORD)


def test_null_topology_matches_only_null():
    a = copy.deepcopy(RECORD)
    a["compat"]["topology"] = None
    b = copy.deepcopy(a)
    assert compat_key(a) == compat_key(b)
    assert compat_key(a) != compat_key(RECORD)
