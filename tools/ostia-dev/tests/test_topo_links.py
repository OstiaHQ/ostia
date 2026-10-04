"""Tests for ostia_dev/topo/links.py: benchmark records to links.json (RFC-0003 §8)."""

import json

import pytest
from ostia_dev.errors import UsageError
from ostia_dev.topo.links import links_from_records

A, B, C = "0000:11:00.0", "0000:12:00.0", "0000:13:00.0"
GIB = 1 << 30


def _link(remote: str, state: str = "active", kind: str = "gpu") -> dict:
    return {"link": 0, "remote_bus_id": remote, "remote_type": kind, "state": state, "version": 4}


@pytest.fixture
def capture(tmp_path):
    gpus = [
        {"bus_id": A, "cuda_ordinal": 0, "nvlinks": [_link(B), _link(C, state="inactive")]},
        {"bus_id": B, "cuda_ordinal": 1, "nvlinks": [_link(A)]},
        {"bus_id": C, "cuda_ordinal": 2, "nvlinks": "not_supported"},
    ]
    (tmp_path / "nvml.json").write_text(json.dumps({"gpus": gpus}), encoding="utf-8")
    return tmp_path


def _record(src: str, dst: str, samples=(100.0,), **params) -> dict:
    p = {"bytes": GIB, "direction": "0->1", "concurrency": 1} | params
    return {
        "bench": "p2p_copy",
        "params": p,
        "unit": "GB/s",
        "samples": list(samples),
        "devices": {"src_bus": src, "dst_bus": dst},
    }


def test_devices_not_ordinals_name_the_endpoints(capture):
    # The ordinals say 0->1, but the program measured B to A.
    out = links_from_records([_record(B, A, direction="0->1")], capture)
    (link,) = out["links"]
    assert (link["from"], link["to"]) == (B, A)


def test_one_entry_with_the_rounded_median_in_mb_per_s(capture):
    out = links_from_records([_record(A, B, samples=(187.2, 187.6, 999.0, 1.0, 187.4))], capture)
    assert out == {
        "schema": 1,
        "links": [
            {
                "from": A,
                "to": B,
                "kind": "nvlink",
                "direction": "forward",
                "bw_mbps": 187400,
                "test": {"bench": "p2p_copy", "bytes": GIB, "concurrency": 1},
                "samples": 5,
            }
        ],
    }


def test_records_of_one_from_to_bytes_pool_their_samples(capture):
    records = [_record(A, B, samples=(10.0, 30.0)), _record(A, B, samples=(20.0,))]
    (link,) = links_from_records(records, capture)["links"]
    assert link["bw_mbps"] == 20000 and link["samples"] == 3


def test_each_size_is_its_own_entry(capture):
    records = [_record(A, B), _record(A, B, bytes=GIB // 4)]
    sizes = [link["test"]["bytes"] for link in links_from_records(records, capture)["links"]]
    assert sizes == [GIB // 4, GIB]


def test_bidirectional_and_other_benchmarks_are_ignored(capture):
    other = _record(A, B) | {"bench": "pipelining"}
    records = [_record(A, B, direction="0<->1"), other]
    assert links_from_records(records, capture)["links"] == []


def test_same_device_records_are_skipped(capture):
    assert links_from_records([_record(A, A)], capture)["links"] == []


def test_kind_comes_from_active_nvlinks(capture):
    records = [_record(A, B), _record(A, C), _record(C, B)]
    kinds = {
        (lk["from"], lk["to"]): lk["kind"] for lk in links_from_records(records, capture)["links"]
    }
    assert kinds == {(A, B): "nvlink", (A, C): "pcie", (C, B): "pcie"}


def test_gpus_on_one_nvswitch_are_nvlinked(tmp_path):
    gpus = [
        {"bus_id": A, "nvlinks": [_link("0000:a1:00.0", kind="switch")]},
        {"bus_id": B, "nvlinks": [_link("0000:a1:00.0", kind="switch")]},
    ]
    (tmp_path / "nvml.json").write_text(json.dumps({"gpus": gpus}), encoding="utf-8")
    (link,) = links_from_records([_record(A, B)], tmp_path)["links"]
    assert link["kind"] == "nvlink"


def test_unmeasured_links_are_absent(capture):
    out = links_from_records([_record(A, B)], capture)
    assert [(lk["from"], lk["to"]) for lk in out["links"]] == [(A, B)]


def test_p2p_copy_without_devices_is_a_usage_error(capture):
    record = _record(A, B)
    del record["devices"]
    with pytest.raises(UsageError) as e:
        links_from_records([record], capture)
    assert "record 1" in e.value.message and "devices.src_bus" in e.value.message


def test_p2p_copy_without_concurrency_is_a_usage_error(capture):
    record = _record(A, B)
    del record["params"]["concurrency"]
    with pytest.raises(UsageError) as e:
        links_from_records([record], capture)
    assert "params.concurrency" in e.value.message


def test_a_gpu_missing_from_the_capture_is_a_usage_error(capture):
    with pytest.raises(UsageError) as e:
        links_from_records([_record(A, "0000:99:00.0")], capture)
    assert "does not list" in e.value.message


def test_a_capture_without_nvml_json_is_a_usage_error(tmp_path):
    with pytest.raises(UsageError) as e:
        links_from_records([], tmp_path)
    assert "nvml.json" in e.value.message
