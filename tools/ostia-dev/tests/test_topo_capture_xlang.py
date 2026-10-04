"""The Python consumer against real ostia-topo-capture output (RFC-0003 §4).

The capture runs on the committed fake machine through the same e2e.py the ctest uses. The tool
is Linux-only: on Linux a missing build fails the test, elsewhere the test is skipped.
"""

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest
from ostia_dev import paths
from ostia_dev.dev import steps
from ostia_dev.topo import manifest
from ostia_dev.topo.manifest import CaptureRejected

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="ostia-topo-capture is Linux-only"
)

E2E = paths.ROOT / "fabric/tools/topo-capture/tests/e2e.py"
DATA = paths.ROOT / "fabric/tests/topology/data"


def _e2e():
    spec = importlib.util.spec_from_file_location("capture_e2e", E2E)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _built() -> tuple[Path, Path]:
    tools = Path(steps.build_root()) / "dev/fabric/tools/topo-capture"
    binary, nvml = tools / "ostia-topo-capture", tools / "fake-nvml"
    if not binary.is_file() or not nvml.is_dir():
        pytest.fail(
            f"ostia-topo-capture or its fake NVML is not built under {tools}\n"
            "  fix: pixi run ostia-dev build"
        )
    return binary, nvml


def _capture(tmp_path_factory, case: str) -> Path:
    binary, nvml = _built()
    out = tmp_path_factory.mktemp(case) / "capture"
    code, out = _e2e().run_case(binary, nvml, DATA, case, out)
    assert code == {"complete": 0, "nvml_error": 2}[case]
    return out


@pytest.fixture(scope="module")
def complete(tmp_path_factory) -> Path:
    return _capture(tmp_path_factory, "complete")


@pytest.fixture
def copy(complete, tmp_path) -> Path:
    d = tmp_path / "capture"
    shutil.copytree(complete, d, symlinks=True)
    return d


def _edit_manifest(d: Path, **changes) -> None:
    doc = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    doc.update(changes)
    (d / "manifest.json").write_text(json.dumps(doc), encoding="utf-8")


def test_the_real_capture_is_accepted(complete):
    doc = manifest.accept(complete)
    assert doc["status"] == "complete"
    assert set(doc["files"]) == {"hwloc.xml", "nvml.json", "nics.json", "meta.json"}


def test_the_real_partial_capture_is_accepted_as_partial(tmp_path_factory):
    doc = manifest.accept(_capture(tmp_path_factory, "nvml_error"))
    assert doc["status"] == "partial"
    assert [m["file"] for m in doc["missing"]] == ["nvml.json"]


def _hash_flip(d: Path) -> None:
    path = d / "nics.json"
    path.write_bytes(path.read_bytes() + b" ")


def _symlink(d: Path) -> None:
    target = d.parent / "meta-elsewhere.json"
    shutil.move(d / "meta.json", target)
    (d / "meta.json").symlink_to(target)


def _extra_file(d: Path) -> None:
    (d / "notes.txt").write_text("x\n", encoding="utf-8")


MUTATIONS = {
    "hash-flip": (_hash_flip, "nics.json does not match its sha256"),
    "symlink": (_symlink, "meta.json is not a regular file"),
    "extra-file": (_extra_file, "1 unexpected entries"),
    "schema-2": (lambda d: _edit_manifest(d, schema=2), "unsupported schema 2 (supported: 1)"),
    "sanitized-false": (lambda d: _edit_manifest(d, sanitized=False), "not sanitized"),
    "leak-check-failed": (lambda d: _edit_manifest(d, leak_check="failed"), "leak_check failed"),
}


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_each_mutation_is_rejected(copy, name):
    mutate, reason = MUTATIONS[name]
    mutate(copy)
    with pytest.raises(CaptureRejected) as e:
        manifest.accept(copy)
    assert reason in e.value.reason
