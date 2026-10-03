"""Tests for ostia_dev/topo/cli.py (RFC-0003 §9, RFC-0005 §7)."""

import subprocess

from ostia_dev.cli import app
from ostia_dev.errors import InfraError, UsageError
from ostia_dev.topo import cli
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
    assert isinstance(r.exception, UsageError) and r.exception.code == 2
    assert "fix:" in r.exception.message


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
    assert calls[-1] == [
        "ctest",
        "--preset",
        "dev",
        "--no-tests=error",
        "-R",
        r"^fabric\.topo\.golden\.",
    ]


def _two_fixtures(monkeypatch, tmp_path):
    for case, name in (("one", "hwloc.xml"), ("pair-tcp", "pair.json")):
        d = tmp_path / "synthetic" / case
        d.mkdir(parents=True)
        (d / name).write_text("x")
    monkeypatch.setattr("ostia_dev.topo.cli.FIXTURES", tmp_path)
    monkeypatch.setattr("ostia_dev.topo.cli.steps.env_or_exit", lambda cmd: "default")
    monkeypatch.setattr("ostia_dev.topo.cli.steps.build_root", lambda: str(tmp_path / "b"))


def test_show_plan_is_configure_build_then_show_with_absolute_path(monkeypatch, tmp_path):
    calls = []
    _two_fixtures(monkeypatch, tmp_path)
    monkeypatch.setattr("ostia_dev.topo.cli.steps.run", lambda plan: calls.extend(plan) or 0)
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(app, ["topo", "show", "synthetic/one"])
    assert r.exit_code == 0
    assert calls[0][:4] == ["pixi", "run", "--frozen", "-e"]
    assert calls[1] == ["cmake", "--build", "--preset", "dev", "--target", "ostia-topo"]
    assert calls[2][1:] == ["show", str((tmp_path / "synthetic/one").resolve())]


def test_relative_path_group_case_and_bare_name_agree(monkeypatch, tmp_path):
    shown = []
    _two_fixtures(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "ostia_dev.topo.cli.steps.run", lambda plan: shown.append(list(plan)[-1][2]) or 0
    )
    monkeypatch.chdir(tmp_path)
    for arg in ("synthetic/one", "one", "./synthetic/one", str(tmp_path / "synthetic/one")):
        assert CliRunner().invoke(app, ["topo", "show", arg]).exit_code == 0
    assert len(set(shown)) == 1


def test_pair_fixture_is_accepted(monkeypatch, tmp_path):
    _two_fixtures(monkeypatch, tmp_path)
    monkeypatch.setattr("ostia_dev.topo.cli.steps.run", lambda plan: 0)
    assert CliRunner().invoke(app, ["topo", "show", "pair-tcp"]).exit_code == 0


def test_ambiguous_case_name_is_a_usage_error(monkeypatch, tmp_path):
    _two_fixtures(monkeypatch, tmp_path)
    other = tmp_path / "real" / "one"
    other.mkdir(parents=True)
    (other / "hwloc.xml").write_text("x")
    r = CliRunner().invoke(app, ["topo", "show", "one"])
    assert isinstance(r.exception, UsageError) and r.exception.code == 2
    assert "synthetic/one" in r.exception.message and "real/one" in r.exception.message


def test_named_fixtures_narrow_the_ctest_regex(monkeypatch, tmp_path):
    calls = []
    _two_fixtures(monkeypatch, tmp_path)
    monkeypatch.setattr("ostia_dev.topo.cli.steps.run", lambda plan: calls.extend(plan) or 0)
    r = CliRunner().invoke(app, ["topo", "golden", "one", "pair-tcp"])
    assert r.exit_code == 0
    assert "--no-tests=error" in calls[-1]
    assert calls[-1][-1] == r"^fabric\.topo\.golden\.(one|pair\-tcp)$"


def test_golden_update_model_failure_changes_nothing(monkeypatch, tmp_path):
    _two_fixtures(monkeypatch, tmp_path)
    monkeypatch.setattr("ostia_dev.topo.cli.steps.run", lambda plan: 0)

    def fake_run(argv, **kw):
        if argv[2].endswith("pair-tcp"):
            raise subprocess.CalledProcessError(3, argv, stderr="cannot replay pair.json")
        return subprocess.CompletedProcess(argv, 0, stdout="{}\n", stderr="")

    monkeypatch.setattr("ostia_dev.topo.cli.subprocess.run", fake_run)
    r = CliRunner().invoke(app, ["topo", "golden", "--update"])
    assert isinstance(r.exception, InfraError) and r.exception.code == 3
    assert "pair-tcp" in r.exception.message
    assert "cannot replay pair.json" in r.exception.message
    assert not list(tmp_path.rglob("expected.json"))


def test_pair_members_are_listed_only_as_the_pair(monkeypatch, tmp_path):
    pair = tmp_path / "captured" / "rdma-pair"
    for node in ("node-0", "node-1"):
        (pair / node).mkdir(parents=True)
        (pair / node / "hwloc.xml").write_text("x")
    (pair / "pair.json").write_text("x")
    monkeypatch.setattr("ostia_dev.topo.cli.FIXTURES", tmp_path)
    assert cli._fixtures() == [pair]


def test_golden_update_outside_fixtures_prints_the_absolute_path(monkeypatch, tmp_path):
    _fake_fixture(monkeypatch, tmp_path / "fixtures")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "hwloc.xml").write_text("<topology/>")
    r = CliRunner().invoke(app, ["topo", "golden", "--update", str(outside)])
    assert r.exit_code == 0, r.output
    assert f"updated {outside.resolve() / 'expected.json'}" in r.output
