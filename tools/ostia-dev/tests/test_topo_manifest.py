"""Tests for ostia_dev/topo/manifest.py, the capture consumer (RFC-0003 §4)."""

import hashlib
import json
import os
import sys

import pytest
from ostia_dev import paths
from ostia_dev.errors import InfraError
from ostia_dev.topo import manifest
from ostia_dev.topo.manifest import CaptureRejected

CORPUS = paths.ROOT / "fabric/tests/topology/data/manifest-corpus"
DATA = {
    "hwloc.xml": "<topology/>\n",
    "nvml.json": '{"gpus": []}\n',
    "nics.json": '{"nics": []}\n',
    "meta.json": '{"provider": "gcp", "instance_type": "g2-standard-16"}\n',
}
TOPO1 = "topo1:sha256:" + "ab" * 32


def _sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def _manifest(**changes) -> dict:
    doc = {
        "schema": 1,
        "tool_version": "0.1.0",
        "status": "complete",
        "sanitized": True,
        "leak_check": "passed",
        "topology_id": TOPO1,
        "files": {name: _sha(text) for name, text in DATA.items()},
        "missing": [],
        "errors": [],
    }
    doc.update(changes)
    return doc


def _write(d, doc: dict) -> None:
    (d / "manifest.json").write_text(json.dumps(doc), encoding="utf-8")


@pytest.fixture
def good(tmp_path):
    d = tmp_path / "capture"
    d.mkdir()
    for name, text in DATA.items():
        (d / name).write_text(text, encoding="utf-8")
    (d / "diagnostics.txt").write_text("hwloc: 4 PCI device(s)\n", encoding="utf-8")
    _write(d, _manifest())
    return d


def _rejected(d) -> str:
    with pytest.raises(CaptureRejected) as e:
        manifest.accept(d)
    return e.value.reason


def test_a_good_capture_is_accepted(good):
    doc = manifest.accept(good)
    assert doc["status"] == "complete" and doc["topology_id"] == TOPO1


def test_diagnostics_txt_is_optional(good):
    (good / "diagnostics.txt").unlink()
    assert manifest.accept(good)["status"] == "complete"


def test_partial_is_accepted_and_reported(good):
    (good / "nvml.json").unlink()
    files = {k: v for k, v in _manifest()["files"].items() if k != "nvml.json"}
    missing = [{"file": "nvml.json", "reason": "nvml_unavailable"}]
    _write(good, _manifest(status="partial", files=files, missing=missing))
    doc = manifest.accept(good)
    assert doc["status"] == "partial"
    assert doc["missing"] == missing


def test_hash_flip_is_rejected(good):
    (good / "nics.json").write_text('{"nics": [1]}\n', encoding="utf-8")
    assert _rejected(good) == "nics.json does not match its sha256 in manifest.json"


def test_symlink_is_rejected(good, tmp_path):
    outside = tmp_path / "meta-copy.json"
    outside.write_text(DATA["meta.json"], encoding="utf-8")
    (good / "meta.json").unlink()
    (good / "meta.json").symlink_to(outside)
    assert _rejected(good) == "meta.json is not a regular file"


def test_symlinked_manifest_is_rejected(good, tmp_path):
    outside = tmp_path / "manifest-copy.json"
    outside.write_bytes((good / "manifest.json").read_bytes())
    (good / "manifest.json").unlink()
    (good / "manifest.json").symlink_to(outside)
    assert "manifest.json is a symlink" in _rejected(good)


def test_extra_file_is_rejected_without_its_name(good):
    (good / "my-hostname.txt").write_text("x", encoding="utf-8")
    reason = _rejected(good)
    assert reason == "the capture holds 1 unexpected entries"
    assert "hostname" not in reason


def test_directory_entry_is_rejected(good):
    (good / "nvml.json").unlink()
    (good / "nvml.json").mkdir()
    assert _rejected(good) == "nvml.json is not a regular file"


def test_missing_listed_file_is_rejected(good):
    (good / "nics.json").unlink()
    assert _rejected(good) == "nics.json is missing"


def test_missing_manifest_is_rejected(good):
    (good / "manifest.json").unlink()
    assert _rejected(good) == "manifest.json is missing"


def test_unsupported_schema_is_named(good):
    _write(good, _manifest(schema=2))
    assert _rejected(good) == "manifest.json has unsupported schema 2 (supported: 1)"


def test_failed_status_is_rejected(good):
    _write(good, _manifest(status="failed", files={}, topology_id=None))
    assert "status failed" in _rejected(good)


def test_unsanitized_is_rejected(good):
    _write(good, _manifest(sanitized=False))
    assert _rejected(good) == "the capture is not sanitized (sanitized false)"


def test_failed_leak_check_is_rejected(good):
    _write(good, _manifest(leak_check="failed"))
    assert _rejected(good) == "the leak check did not pass (leak_check failed)"


def test_leak_check_not_run_is_rejected(good):
    _write(good, _manifest(leak_check="not_run"))
    assert _rejected(good) == "the leak check did not pass (leak_check not_run)"


def test_unknown_key_is_rejected(good):
    doc = _manifest()
    doc["hostname"] = "x"
    _write(good, doc)
    assert _rejected(good) == (
        "manifest.json does not match its schema at $ (additionalProperties)"
    )


def test_path_outside_the_contract_is_rejected(good):
    files = _manifest()["files"] | {"../x": _sha("x")}
    _write(good, _manifest(files=files))
    assert _rejected(good).startswith("manifest.json lists a file outside hwloc.xml")


def test_null_topology_id_is_rejected(good):
    _write(good, _manifest(topology_id=None))
    assert _rejected(good) == "manifest.json has no topology_id"


def test_a_capture_without_meta_json_is_rejected(good):
    files = {k: v for k, v in _manifest()["files"].items() if k != "meta.json"}
    (good / "meta.json").unlink()
    _write(good, _manifest(files=files))
    assert _rejected(good) == "manifest.json does not list meta.json"


def test_rejections_are_distinct(good):
    reasons = set()
    for change in (
        {"schema": 2},
        {"status": "failed", "files": {}, "topology_id": None},
        {"sanitized": False},
        {"leak_check": "failed"},
        {"status": "bogus"},
    ):
        _write(good, _manifest(**change))
        reasons.add(_rejected(good))
    assert len(reasons) == 5


@pytest.mark.parametrize(
    "raw",
    [b"\xff\xfe", b"{", b"[]", b'{"schema": NaN}', b'{"schema": 1, "schema": 1}'],
)
def test_malformed_manifest_bytes_are_rejected(raw):
    with pytest.raises(CaptureRejected):
        manifest.check_manifest(raw)


def test_a_fifo_is_not_read(good):
    if not hasattr(os, "mkfifo"):
        pytest.skip("no FIFOs on this platform")
    (good / "hwloc.xml").unlink()
    os.mkfifo(good / "hwloc.xml")
    assert _rejected(good) == "hwloc.xml is not a regular file"


def test_missing_jsonschema_is_an_infra_error_naming_pixi_install(monkeypatch, good):
    monkeypatch.setitem(sys.modules, "jsonschema", None)
    with pytest.raises(InfraError) as e:
        manifest.accept(good)
    assert e.value.code == 3
    assert "fix: pixi install" in e.value.message


def _corpus():
    cases = sorted(CORPUS.glob("*.json"))
    assert len(cases) >= 10
    return cases


@pytest.mark.parametrize("path", _corpus(), ids=lambda p: p.stem)
def test_corpus_agrees_with_the_cpp_validator(path):
    """The Python half of the validator agreement; manifest_test.cpp is the C++ half."""
    case = json.loads(path.read_text(encoding="utf-8"))
    assert path.name.startswith(case["expect"] + "-")
    doc = json.loads(case["manifest_text"]) if "manifest_text" in case else case["manifest"]
    try:
        manifest.validate(doc)
        verdict = "valid"
    except CaptureRejected:
        verdict = "invalid"
    assert verdict == case["expect"]


def test_the_consumer_rejects_the_duplicate_key_case_before_validating():
    case = json.loads((CORPUS / "valid-duplicate-key.json").read_text(encoding="utf-8"))
    with pytest.raises(CaptureRejected) as e:
        manifest.check_manifest(case["manifest_text"].encode())
    assert e.value.reason == "manifest.json repeats a key"


def test_trailing_newline_after_a_pattern_is_rejected():
    with pytest.raises(CaptureRejected) as e:
        manifest.validate(_manifest(topology_id=TOPO1 + "\n"))
    assert "$.topology_id (pattern)" in e.value.reason


def test_a_float_schema_version_equal_to_one_is_accepted():
    manifest.validate(_manifest(schema=1.0))
