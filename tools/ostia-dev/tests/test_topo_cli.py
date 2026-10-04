"""Tests for ostia_dev/topo/cli.py (RFC-0003 §9, RFC-0005 §7)."""

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest
from ostia_dev.cli import app
from ostia_dev.errors import InfraError, OstiaError, UsageError
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


CAPTURE_TOOL = "dev/fabric/tools/topo-capture/ostia-topo-capture"


def _linux(monkeypatch, tmp_path, code=0, manifest=None, diagnostics=""):
    """topo capture on a pretend Linux host whose tool writes `manifest` into --out."""
    seen = {"plans": [], "argv": None, "ids": None, "mode": None}
    monkeypatch.setattr("ostia_dev.topo.cli._on_linux", lambda: True)
    monkeypatch.setenv("PIXI_ENVIRONMENT_NAME", "default")
    monkeypatch.delenv("OSTIA_TOPO_CAPTURE", raising=False)
    for var in ("NODE_NAME", "OSTIA_LEAK_IDENTIFIERS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OSTIA_CAPTURE_PROVIDER", "gcp")
    monkeypatch.setenv("OSTIA_CAPTURE_INSTANCE_TYPE", "g2-standard-16")
    monkeypatch.setattr("ostia_dev.topo.cli.steps.build_root", lambda: str(tmp_path / "b"))
    monkeypatch.setattr(
        "ostia_dev.topo.cli.steps.run", lambda plan: seen["plans"].append(list(plan)) or 0
    )
    built = tmp_path / "b" / CAPTURE_TOOL
    built.parent.mkdir(parents=True, exist_ok=True)
    built.write_text("#!/bin/sh\n")
    built.chmod(0o755)

    def tool(argv):
        seen["argv"] = argv
        if "--extra-identifiers" in argv:
            path = Path(argv[argv.index("--extra-identifiers") + 1])
            seen["ids"] = path.read_text().splitlines()
            seen["mode"] = stat.S_IMODE(path.stat().st_mode)
            seen["ids_path"] = path
        if "--out" in argv and manifest is not None:
            out = Path(argv[argv.index("--out") + 1])
            out.mkdir(parents=True, exist_ok=True)
            (out / "manifest.json").write_text(json.dumps(manifest))
            (out / "diagnostics.txt").write_text(diagnostics)
        return code

    monkeypatch.setattr("ostia_dev.topo.cli._run_tool", tool)
    return seen


def test_capture_off_linux_points_at_the_remote_suite(monkeypatch):
    monkeypatch.setattr("ostia_dev.topo.cli._on_linux", lambda: False)
    r = CliRunner().invoke(app, ["topo", "capture", "--out", "x"])
    assert isinstance(r.exception, UsageError) and r.exception.code == 2
    fix = next(line for line in r.exception.message.splitlines() if "fix:" in line)
    assert "remote k8s --profile l4 --suite topo-capture" in fix


def test_capture_builds_the_tool_and_forwards_arguments(monkeypatch, tmp_path):
    seen = _linux(monkeypatch, tmp_path, manifest={"status": "complete"})
    out = tmp_path / "cap"
    args = ["topo", "capture", "--out", str(out), "--node-index", "1", "--no-verbs"]
    r = CliRunner().invoke(app, args)
    assert r.exit_code == 0, r.output
    assert ["cmake", "--build", "--preset", "dev", "--target", "ostia-topo-capture"] in seen[
        "plans"
    ][0]
    argv = seen["argv"]
    assert argv[0] == str(tmp_path / "b" / CAPTURE_TOOL)
    assert argv[1:3] == ["--out", str(out)]
    assert argv[-3:] == ["--node-index", "1", "--no-verbs"]
    assert argv[argv.index("--provider") + 1] == "gcp"
    assert argv[argv.index("--instance-type") + 1] == "g2-standard-16"
    assert "--extra-identifiers" not in argv


def test_capture_flags_override_the_environment(monkeypatch, tmp_path):
    seen = _linux(monkeypatch, tmp_path)
    r = CliRunner().invoke(
        app, ["topo", "capture", "--print-id", "--provider", "aws", "--instance-type", "g6"]
    )
    assert r.exit_code == 0, r.output
    argv = seen["argv"]
    assert argv[argv.index("--provider") + 1] == "aws"
    assert argv[argv.index("--instance-type") + 1] == "g6"


def test_capture_uses_build_dir_without_building(monkeypatch, tmp_path):
    seen = _linux(monkeypatch, tmp_path)
    tool = tmp_path / "tree" / "fabric/tools/topo-capture/ostia-topo-capture"
    tool.parent.mkdir(parents=True)
    tool.write_text("#!/bin/sh\n")
    r = CliRunner().invoke(
        app, ["topo", "capture", "--print-id", "--build-dir", str(tool.parents[3])]
    )
    assert r.exit_code == 0, r.output
    assert seen["plans"] == []
    assert seen["argv"][0] == str(tool.resolve())


def test_capture_missing_build_dir_tool_is_a_usage_error(monkeypatch, tmp_path):
    _linux(monkeypatch, tmp_path)
    r = CliRunner().invoke(app, ["topo", "capture", "--print-id", "--build-dir", str(tmp_path)])
    assert isinstance(r.exception, UsageError) and r.exception.code == 2


def test_capture_writes_a_private_identifiers_file_and_removes_it(monkeypatch, tmp_path):
    seen = _linux(monkeypatch, tmp_path)
    monkeypatch.setenv("NODE_NAME", "gke-node-abcdef")
    monkeypatch.setenv("OSTIA_LEAK_IDENTIFIERS", "project-123456\n  Rack 42 Site North  \n\n")
    r = CliRunner().invoke(app, ["topo", "capture", "--print-id"])
    assert r.exit_code == 0, r.output
    # A multi-word identifier reaches the tool as one line, which it matches as one substring.
    assert seen["ids"] == ["gke-node-abcdef", "project-123456", "Rack 42 Site North"]
    assert seen["mode"] == 0o600
    assert not seen["ids_path"].exists()


def test_capture_appends_pod_identifiers_to_the_callers_file(monkeypatch, tmp_path):
    seen = _linux(monkeypatch, tmp_path)
    monkeypatch.setenv("NODE_NAME", "gke-node-abcdef")
    mine = tmp_path / "mine.txt"
    mine.write_text("site-rack-42\n")
    r = CliRunner().invoke(app, ["topo", "capture", "--print-id", "--extra-identifiers", str(mine)])
    assert r.exit_code == 0, r.output
    assert seen["ids"] == ["site-rack-42", "gke-node-abcdef"]
    assert mine.exists()


def _manifest(status, leak="passed", missing=(), errors_=()):
    return {"status": status, "leak_check": leak, "missing": list(missing), "errors": list(errors_)}


@pytest.mark.parametrize(
    ("code", "doc", "diagnostics", "line"),
    [
        (0, _manifest("complete"), "", "capture complete: "),
        (
            2,
            _manifest("partial", missing=[{"file": "nvml.json", "reason": "nvml_init"}]),
            "",
            "capture partial: nvml.json missing (nvml_init), see ",
        ),
        (
            3,
            _manifest("failed", leak="failed", errors_=[{"code": "leak", "message": "m"}]),
            "hwloc: 1\nleak: nics.json:3: ib-guid\nleak: meta.json:2: hostname\n",
            "capture rejected by the leak check: 2 findings, see ",
        ),
        (
            1,
            _manifest("failed", leak="not_run", errors_=[{"code": "xml_reimport", "message": "m"}]),
            "",
            "capture failed: xml_reimport, see ",
        ),
    ],
)
def test_capture_passes_the_exit_code_and_interprets_it(
    monkeypatch, tmp_path, code, doc, diagnostics, line
):
    _linux(monkeypatch, tmp_path, code=code, manifest=doc, diagnostics=diagnostics)
    out = tmp_path / "cap"
    r = CliRunner().invoke(app, ["topo", "capture", "--out", str(out)])
    assert r.exit_code == code
    assert not isinstance(r.exception, OstiaError)
    assert line in r.stderr
    if code:
        assert str(out / "diagnostics.txt") in r.stderr


def test_capture_ignores_a_stale_manifest(monkeypatch, tmp_path):
    out = tmp_path / "cap"
    out.mkdir()
    (out / "manifest.json").write_text(json.dumps(_manifest("failed", errors_=[{"code": "old"}])))
    os.utime(out / "manifest.json", ns=(0, 0))
    _linux(monkeypatch, tmp_path, code=1)
    r = CliRunner().invoke(app, ["topo", "capture", "--out", str(out)])
    assert r.exit_code == 1
    assert "capture failed: no manifest written, see the error above" in r.stderr


def _diff_runner(monkeypatch, tmp_path, code):
    calls = []
    _two_fixtures(monkeypatch, tmp_path)

    def run(plan):
        plan = list(plan)
        calls.extend(plan)
        return code if plan[-1][1:2] == ["diff"] else 0

    monkeypatch.setattr("ostia_dev.topo.cli.steps.run", run)
    return calls


def test_diff_passes_exit_1_through(monkeypatch, tmp_path):
    calls = _diff_runner(monkeypatch, tmp_path, 1)
    r = CliRunner().invoke(app, ["topo", "diff", "one", "pair-tcp"])
    assert r.exit_code == 1
    assert not isinstance(r.exception, OstiaError)
    assert ["cmake", "--build", "--preset", "dev", "--target", "ostia-topo"] in calls
    assert calls[-1][1:] == [
        "diff",
        str((tmp_path / "synthetic/one").resolve()),
        str((tmp_path / "synthetic/pair-tcp").resolve()),
    ]


def test_diff_equal_ids_exit_0(monkeypatch, tmp_path):
    _diff_runner(monkeypatch, tmp_path, 0)
    assert CliRunner().invoke(app, ["topo", "diff", "one", "one"]).exit_code == 0


@pytest.mark.parametrize("code", [2, 3])
def test_diff_tool_errors_are_infra_errors(monkeypatch, tmp_path, code):
    _diff_runner(monkeypatch, tmp_path, code)
    r = CliRunner().invoke(app, ["topo", "diff", "one", "pair-tcp"])
    assert isinstance(r.exception, InfraError) and r.exception.code == 3
    assert f"exit code: {code}" in r.exception.message


def _which(monkeypatch, tmp_path):
    _two_fixtures(monkeypatch, tmp_path)
    monkeypatch.setattr("ostia_dev.topo.cli.steps.run", lambda plan: 0)
    ids = {"one": "topo1:sha256:" + "ab" * 32, "pair-tcp": "topo1:sha256:" + "cd" * 32}
    monkeypatch.setattr("ostia_dev.topo.cli._id", lambda d: ids[d.name])


def test_which_lists_the_fixtures_with_that_id(monkeypatch, tmp_path):
    _which(monkeypatch, tmp_path)
    r = CliRunner().invoke(app, ["topo", "which", "topo1:sha256:" + "ab" * 32])
    assert r.exit_code == 0, r.output
    assert r.stdout.splitlines() == [f"synthetic/one  topo1:sha256:{'ab' * 32}"]


def test_which_accepts_a_short_id(monkeypatch, tmp_path):
    _which(monkeypatch, tmp_path)
    r = CliRunner().invoke(app, ["topo", "which", "topo1:sha256:cdcdcd"])
    assert r.exit_code == 0, r.output
    assert r.stdout.startswith("synthetic/pair-tcp  ")


def test_which_without_a_match_exits_1(monkeypatch, tmp_path):
    _which(monkeypatch, tmp_path)
    r = CliRunner().invoke(app, ["topo", "which", "ef01"])
    assert r.exit_code == 1
    assert "no fixture has topology topo1:sha256:ef01" in r.stderr


def test_which_rejects_a_non_id(monkeypatch, tmp_path):
    _which(monkeypatch, tmp_path)
    r = CliRunner().invoke(app, ["topo", "which", "sha1:xyz"])
    assert isinstance(r.exception, UsageError) and r.exception.code == 2


def test_capture_removes_the_identifiers_file_when_interrupted(monkeypatch, tmp_path):
    _linux(monkeypatch, tmp_path)
    monkeypatch.setenv("NODE_NAME", "gke-node-abcdef")
    made = []

    def interrupted(argv):
        made.append(Path(argv[argv.index("--extra-identifiers") + 1]))
        raise KeyboardInterrupt

    monkeypatch.setattr("ostia_dev.topo.cli._run_tool", interrupted)
    CliRunner().invoke(app, ["topo", "capture", "--print-id"])
    assert made and not made[0].exists()


def test_capture_non_executable_override_is_a_usage_error(monkeypatch, tmp_path):
    _linux(monkeypatch, tmp_path)
    tool = tmp_path / "not-executable"
    tool.write_text("x")
    tool.chmod(0o644)
    monkeypatch.setenv("OSTIA_TOPO_CAPTURE", str(tool))
    r = CliRunner().invoke(app, ["topo", "capture", "--print-id"])
    assert isinstance(r.exception, UsageError) and r.exception.code == 2
    assert "$OSTIA_TOPO_CAPTURE names no executable" in r.exception.message
    assert "fix:" in r.exception.message


def test_capture_missing_built_tool_is_a_usage_error(monkeypatch, tmp_path):
    _linux(monkeypatch, tmp_path)
    (tmp_path / "b" / CAPTURE_TOOL).unlink()
    r = CliRunner().invoke(app, ["topo", "capture", "--print-id"])
    assert isinstance(r.exception, UsageError) and r.exception.code == 2


def test_capture_does_not_echo_a_non_code_reason(monkeypatch, tmp_path):
    doc = _manifest("partial", missing=[{"file": "nvml.json", "reason": "host-a.example 10.0.0.1"}])
    _linux(monkeypatch, tmp_path, code=2, manifest=doc)
    r = CliRunner().invoke(app, ["topo", "capture", "--out", str(tmp_path / "cap")])
    assert r.exit_code == 2
    assert "nvml.json missing (non-code reason)" in r.stderr
    assert "example" not in r.stderr


def test_diff_passes_an_interrupt_through(monkeypatch, tmp_path):
    _diff_runner(monkeypatch, tmp_path, 130)
    r = CliRunner().invoke(app, ["topo", "diff", "one", "pair-tcp"])
    assert r.exit_code == 130
    assert not isinstance(r.exception, OstiaError)
