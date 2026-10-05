"""Tests for `ostia-dev topo import` and ostia_dev/topo/importer.py (RFC-0003 §9)."""

import hashlib
import json

import pytest
from ostia_dev.cli import app
from ostia_dev.errors import CheckFailed, UsageError
from typer.testing import CliRunner

TOPO1 = "topo1:sha256:" + "ab" * 32
GOLDEN = '{"nodes":[]}\n'


def _capture(tmp_path, provider="aws", instance_type="g6.4xlarge", status="complete"):
    d = tmp_path / "capture"
    d.mkdir()
    meta = {"schema": 1, "provider": provider, "instance_type": instance_type}
    data = {
        "hwloc.xml": "<topology/>\n",
        "nics.json": '{"nics": []}\n',
        "meta.json": json.dumps(meta) + "\n",
    }
    if status == "complete":
        data["nvml.json"] = '{"gpus": []}\n'
    for name, text in data.items():
        (d / name).write_text(text, encoding="utf-8")
    (d / "diagnostics.txt").write_text("hwloc: 1 PCI device(s)\n", encoding="utf-8")
    files = {n: "sha256:" + hashlib.sha256(t.encode()).hexdigest() for n, t in data.items()}
    missing = [] if status == "complete" else [{"file": "nvml.json", "reason": "nvml_init"}]
    manifest = {
        "schema": 1,
        "tool_version": "0.1.0",
        "status": status,
        "sanitized": True,
        "leak_check": "passed",
        "topology_id": TOPO1,
        "files": files,
        "missing": missing,
        "errors": [],
    }
    (d / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return d


@pytest.fixture
def fake(monkeypatch, tmp_path):
    """Fixtures under tmp_path/fixtures, a recording runner and a fake ostia-topo model."""
    seen = {"plans": [], "leaks": 0}
    fixtures = tmp_path / "fixtures"
    monkeypatch.setattr("ostia_dev.topo.cli.FIXTURES", fixtures)
    monkeypatch.setattr("ostia_dev.topo.cli.steps.env_or_exit", lambda cmd: "default")
    monkeypatch.setattr("ostia_dev.topo.cli._model", lambda d: GOLDEN)

    def run(plan):
        plan = list(plan)
        seen["plans"].append(plan)
        leaks = any("ostia_dev.ci.check_fixture_leaks" in step for step in plan)
        return seen["leaks"] if leaks else 0

    monkeypatch.setattr("ostia_dev.topo.cli.steps.run", run)
    seen["fixtures"] = fixtures
    return seen


def test_import_names_the_fixture_from_meta_json(fake, tmp_path):
    r = CliRunner().invoke(app, ["topo", "import", str(_capture(tmp_path))])
    assert r.exit_code == 0, r.output
    target = fake["fixtures"] / "captured" / "aws-g6-4xlarge"
    assert sorted(p.name for p in target.iterdir()) == [
        "expected.json",
        "hwloc.xml",
        "manifest.json",
        "meta.json",
        "nics.json",
        "nvml.json",
    ]
    assert (target / "expected.json").read_text() == GOLDEN
    assert f"imported captured/aws-g6-4xlarge ({TOPO1})" in r.stdout


def test_import_copies_only_listed_files_and_the_manifest(fake, tmp_path):
    capture = _capture(tmp_path)
    r = CliRunner().invoke(app, ["topo", "import", str(capture)])
    assert r.exit_code == 0, r.output
    target = fake["fixtures"] / "captured" / "aws-g6-4xlarge"
    assert not (target / "diagnostics.txt").exists()
    for name in ("hwloc.xml", "meta.json", "manifest.json"):
        assert (target / name).read_bytes() == (capture / name).read_bytes()


def test_import_name_option_wins(fake, tmp_path):
    r = CliRunner().invoke(app, ["topo", "import", str(_capture(tmp_path)), "--name", "gcp-l4"])
    assert r.exit_code == 0, r.output
    assert (fake["fixtures"] / "captured" / "gcp-l4" / "manifest.json").is_file()


def test_import_builds_ostia_topo_and_runs_fixture_leaks_on_the_new_files(fake, tmp_path):
    r = CliRunner().invoke(app, ["topo", "import", str(_capture(tmp_path))])
    assert r.exit_code == 0, r.output
    build, leaks = fake["plans"]
    assert build[-1] == ["cmake", "--build", "--preset", "dev", "--target", "ostia-topo"]
    (step,) = leaks
    assert step[1:4] == ["-m", "ostia_dev.ci.check_fixture_leaks", "--files"]
    target = fake["fixtures"] / "captured" / "aws-g6-4xlarge"
    assert sorted(step[4:]) == sorted(str(p) for p in target.iterdir())


def test_import_prints_the_review_checklist(fake, tmp_path):
    r = CliRunner().invoke(app, ["topo", "import", str(_capture(tmp_path))])
    assert r.exit_code == 0, r.output
    assert "review before you commit captured/aws-g6-4xlarge:" in r.stdout
    assert "pixi run ostia-dev topo show captured/aws-g6-4xlarge" in r.stdout
    assert r.stdout.count("  [ ] ") >= 4


def test_import_refuses_a_partial_capture(fake, tmp_path):
    r = CliRunner().invoke(app, ["topo", "import", str(_capture(tmp_path, status="partial"))])
    assert isinstance(r.exception, CheckFailed) and r.exception.code == 1
    assert "partial, not complete" in r.exception.message
    assert "missing: nvml.json (nvml_init)" in r.exception.message
    assert not fake["fixtures"].exists()


def test_import_echoes_no_manifest_string_that_is_not_a_file_or_code(fake, tmp_path):
    capture = _capture(tmp_path, status="partial")
    doc = json.loads((capture / "manifest.json").read_text())
    doc["missing"] = [{"file": "host-a.example", "reason": "10.0.0.1 rack-7"}]
    (capture / "manifest.json").write_text(json.dumps(doc))
    r = CliRunner().invoke(app, ["topo", "import", str(capture)])
    assert isinstance(r.exception, CheckFailed)
    assert "missing: non-data file (non-code reason)" in r.exception.message
    assert "example" not in r.exception.message and "10.0.0.1" not in r.exception.message


def test_import_refuses_a_rejected_capture(fake, tmp_path):
    capture = _capture(tmp_path)
    (capture / "extra.txt").write_text("x")
    r = CliRunner().invoke(app, ["topo", "import", str(capture)])
    assert isinstance(r.exception, CheckFailed) and r.exception.code == 1
    assert "1 unexpected entries" in r.exception.message


def test_import_removes_the_folder_when_fixture_leaks_finds_something(fake, tmp_path):
    fake["leaks"] = 1
    r = CliRunner().invoke(app, ["topo", "import", str(_capture(tmp_path))])
    assert isinstance(r.exception, CheckFailed) and r.exception.code == 1
    assert not (fake["fixtures"] / "captured" / "aws-g6-4xlarge").exists()
    fix = next(line for line in r.exception.message.splitlines() if "fix:" in line)
    names = sorted(part.rpartition("/")[2] for part in fix.split("--files ", 1)[1].split())
    assert names == ["hwloc.xml", "manifest.json", "meta.json", "nics.json", "nvml.json"]


def test_import_never_overwrites_a_fixture(fake, tmp_path):
    existing = fake["fixtures"] / "captured" / "aws-g6-4xlarge"
    existing.mkdir(parents=True)
    (existing / "hwloc.xml").write_text("old")
    r = CliRunner().invoke(app, ["topo", "import", str(_capture(tmp_path))])
    assert isinstance(r.exception, UsageError) and r.exception.code == 2
    assert (existing / "hwloc.xml").read_text() == "old"


def test_import_needs_a_name_when_meta_json_says_unknown(fake, tmp_path):
    r = CliRunner().invoke(app, ["topo", "import", str(_capture(tmp_path, provider="unknown"))])
    assert isinstance(r.exception, UsageError) and r.exception.code == 2
    assert "--name" in r.exception.message


def test_import_rejects_a_name_with_path_separators(fake, tmp_path):
    r = CliRunner().invoke(app, ["topo", "import", str(_capture(tmp_path)), "--name", "../x"])
    assert isinstance(r.exception, UsageError) and r.exception.code == 2
    assert not fake["fixtures"].exists()
