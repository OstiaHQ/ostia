"""Tests for ostia_dev/topo/cli.py (RFC-0003 §9, RFC-0005 §7)."""

from ostia_dev.cli import app
from typer.testing import CliRunner


def test_show_builds_then_runs_ostia_topo(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr("ostia_dev.topo.cli.steps.env_or_exit", lambda cmd: "default")
    monkeypatch.setattr("ostia_dev.topo.cli.steps.build_root", lambda: str(tmp_path))
    monkeypatch.setattr("ostia_dev.topo.cli.steps.run", lambda plan: calls.extend(plan) or 0)
    r = CliRunner().invoke(app, ["topo", "show", "fabric/tests/fixtures/topology/synthetic/no-nic"])
    assert r.exit_code == 0
    assert ["cmake", "--build", "--preset", "dev", "--target", "ostia-topo"] in calls
    assert calls[-1][0] == str(tmp_path / "dev/fabric/tools/topo/ostia-topo")
    assert calls[-1][1] == "show"


def test_show_missing_fixture_is_a_usage_error(monkeypatch):
    monkeypatch.setattr("ostia_dev.topo.cli.steps.env_or_exit", lambda cmd: "default")
    r = CliRunner().invoke(app, ["topo", "show", "no/such/dir"])
    assert r.exit_code == 2
    assert "fix:" in r.output


def _fake_fixture(monkeypatch, tmp_path):
    fixture = tmp_path / "synthetic" / "case"
    fixture.mkdir(parents=True)
    (fixture / "hwloc.xml").write_text("<topology/>")
    monkeypatch.setattr("ostia_dev.topo.cli.FIXTURES", tmp_path)
    monkeypatch.setattr("ostia_dev.topo.cli.steps.env_or_exit", lambda cmd: "default")
    monkeypatch.setattr("ostia_dev.topo.cli.steps.run", lambda plan: 0)
    monkeypatch.setattr("ostia_dev.topo.cli._model", lambda d: '{"nodes":[]}\n')
    return fixture


def test_golden_update_writes_expected_json(monkeypatch, tmp_path):
    fixture = _fake_fixture(monkeypatch, tmp_path)
    r = CliRunner().invoke(app, ["topo", "golden", "--update"])
    assert r.exit_code == 0
    assert (fixture / "expected.json").read_text() == '{"nodes":[]}\n'
    assert "updated synthetic/case/expected.json" in r.output


def test_golden_update_leaves_matching_files_alone(monkeypatch, tmp_path):
    fixture = _fake_fixture(monkeypatch, tmp_path)
    (fixture / "expected.json").write_text('{"nodes":[]}\n')
    before = (fixture / "expected.json").stat().st_mtime_ns
    r = CliRunner().invoke(app, ["topo", "golden", "--update"])
    assert r.exit_code == 0
    assert "unchanged synthetic/case/expected.json" in r.output
    assert (fixture / "expected.json").stat().st_mtime_ns == before


def test_golden_runs_ctest_by_default(monkeypatch, tmp_path):
    calls = []
    _fake_fixture(monkeypatch, tmp_path)
    monkeypatch.setattr("ostia_dev.topo.cli.steps.run", lambda plan: calls.extend(plan) or 0)
    r = CliRunner().invoke(app, ["topo", "golden"])
    assert r.exit_code == 0
    assert calls[-1] == ["ctest", "--preset", "dev", "-R", r"^fabric\.topo\.golden\."]
