"""The shared pipeline driver and exit codes (RFC-0005 §3.1, §3.5; R13, R16)."""

import datetime
import io
import json
import tarfile
from pathlib import Path

import pytest

from ostia_dev import config
from ostia_dev.errors import InfraError, UsageError
from ostia_dev.remote import core

JUNIT = Path(__file__).parent / "fixtures" / "junit"


def test_run_id_format():
    now = datetime.datetime(2026, 10, 2, 14, 15, 1, tzinfo=datetime.UTC)
    assert core.run_id("k8s", "l4", now, "a1b2c3") == "k8s-l4-20261002-141501-a1b2c3"
    rid = core.run_id("container", "cpu", now)
    assert rid.startswith("container-cpu-20261002-141501-") and len(rid.split("-")[-1]) == 6


@pytest.mark.parametrize("key", ["GH_TOKEN", "aws_secret_access_key", "API_KEY", "DB_PASSWORD"])
def test_failure_secret_looking_env_var(key):
    with pytest.raises(UsageError) as e:
        core.check_env_vars([f"{key}=x"], allow_secret=False)
    assert key in e.value.message and "--allow-secret" in e.value.message
    assert core.check_env_vars([f"{key}=x"], allow_secret=True) == {key: "x"}


def test_env_vars_parse():
    assert core.check_env_vars(["A=1", "B=x=y"], allow_secret=False) == {"A": "1", "B": "x=y"}
    with pytest.raises(UsageError):
        core.check_env_vars(["NOEQUALS"], allow_secret=False)


@pytest.mark.parametrize(("cache", "env_vars"), [(True, {}), (False, {"A": "1"})])
def test_failure_ref_pr_with_cache_or_env_var(cache, env_vars):
    with pytest.raises(UsageError) as e:
        core.check_ref_rules("pr/12", cache=cache, env_vars=env_vars)
    assert "RFC-0005 §4.10" in e.value.message
    core.check_ref_rules("3f2a9c1", cache=cache, env_vars=env_vars)  # a SHA is fine


# §3.5, one row per case: (test_code, infra, verified, interrupted, usage) -> exit
FINAL = [
    ((0, False, True, False, False), 0),
    ((1, False, True, False, False), 1),
    ((0, True, True, False, False), 3),
    ((0, False, False, False, False), 4),
    ((1, False, False, False, False), 1),  # the test result wins over 4
    ((0, True, False, False, False), 3),
    ((0, False, True, True, False), 130),
    ((1, True, False, True, False), 130),  # a second Ctrl-C: delete requested, not verified
    ((0, False, True, False, True), 2),
    ((0, False, False, False, True), 2),
]


@pytest.mark.parametrize(("args", "code"), FINAL)
def test_final_exit(args, code):
    t, infra, verified, interrupted, usage = args
    assert core.final_exit(t, infra, verified, interrupted, usage=usage) == code


@pytest.mark.parametrize(("codes", "worst"), [([0, 0], 0), ([0, 1], 1), ([4, 1], 1),
                                              ([1, 3], 3), ([0, 4], 4), ([3, 130], 130)])
def test_worst_exit(codes, worst):
    assert core.worst(codes) == worst


# A fake backend whose "pod" is a directory: the supervisor's outputs are scripted.


def _tar_of(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _steps(*codes):
    names = ["install", "configure", "build", "command"]
    kinds = ["install", "build", "build", "command"]
    return {
        "schema": 1,
        "state": "done",
        "code_wait": "ok",
        "exit": 0,
        "steps": [
            {"name": n, "kind": k, "code": c, "seconds": 2, "result": "ok" if c == 0 else "failed"}
            for n, k, c in zip(names, kinds, codes, strict=False)
        ],
    }


class FakeBackend:
    name = "container"

    def __init__(self, steps=None, junit="pass.xml", fail_at=None, verified=True):
        self.calls: list[tuple[str, str]] = []
        self.steps = steps if steps is not None else _steps(0, 0, 0, 0)
        self.junit = junit
        self.fail_at = fail_at  # (op, exception)
        self.verified = verified

    def _op(self, op, run):
        self.calls.append((op, run.env if run else ""))
        if self.fail_at and self.fail_at[0] == op:
            raise self.fail_at[1]

    def gc(self, run):
        self._op("gc", run)
        return []

    def prepare(self, run):
        self._op("prepare", run)

    def start(self, run):
        self._op("start", run)

    def upload(self, run):
        self._op("upload", run)

    def stream(self, run):
        self._op("stream", run)

    def collect(self, run, workdir):
        self._op("collect", run)
        control = workdir / "control.tar"
        files = {"steps.json": json.dumps(self.steps).encode(), "log.txt": b"log\n"}
        control.write_bytes(_tar_of(files))
        artifacts = workdir / "artifacts.tar"
        build = run.plan.build_dir.removeprefix("/w/")
        arts = {f"{build}/junit.xml": (JUNIT / self.junit).read_bytes()} if self.junit else {}
        artifacts.write_bytes(_tar_of(arts))
        return control, artifacts

    def teardown(self, run):
        self._op("teardown", run)
        return self.verified

    def describe(self, run):
        return {"engine": "fake"}


@pytest.fixture
def cfg(tmp_path):
    return config.load(tmp_path / "none.toml")


@pytest.fixture
def repo(git_repo, monkeypatch):
    monkeypatch.setattr(core, "check_lock", lambda root: None)  # git_repo has no pixi.lock
    return git_repo


def _spec(tmp_path, **kw):
    base = dict(backend="container", profile="cpu", envs=["default"], results=tmp_path / "results")
    base.update(kw)
    return core.RunSpec(**base)


def test_drive_runs_the_operations_in_order_and_passes(cfg, repo, tmp_path, capsys):
    b = FakeBackend()
    code = core.drive(b, _spec(tmp_path), cfg=cfg, repo=repo)
    assert code == 0
    assert [op for op, _ in b.calls] == [
        "prepare", "gc", "start", "upload", "stream", "collect", "teardown",
    ]  # fmt: skip
    (run_dir,) = (tmp_path / "results").iterdir()
    summary = json.loads((run_dir / "summary.json").read_text())
    assert summary["exit_code"] == 0 and summary["teardown"] == "verified"
    assert summary["result"] == "passed" and summary["env"] == "default"
    assert summary["parallelism"] == {"build_jobs": 4, "test_jobs": 4}
    assert (run_dir / "junit.xml").exists() and (run_dir / "log.txt").exists()
    assert run_dir.name in capsys.readouterr().out  # the summary line


def test_drive_failed_tests_exit_1(cfg, repo, tmp_path):
    b = FakeBackend(steps=_steps(0, 0, 0, 8), junit="failed.xml")
    assert core.drive(b, _spec(tmp_path), cfg=cfg, repo=repo) == 1
    assert b.calls[-1][0] == "teardown"


def test_drive_zero_tests_exit_1(cfg, repo, tmp_path):
    b = FakeBackend(junit="zero-tests.xml")
    assert core.drive(b, _spec(tmp_path), cfg=cfg, repo=repo) == 1


@pytest.mark.parametrize("op", ["start", "upload", "stream", "collect"])
def test_teardown_runs_after_an_infra_failure(cfg, repo, tmp_path, op):
    b = FakeBackend(fail_at=(op, InfraError("error: boom")))
    assert core.drive(b, _spec(tmp_path), cfg=cfg, repo=repo) == 3
    assert b.calls[-1][0] == "teardown"


@pytest.mark.parametrize("op", ["start", "stream"])
def test_teardown_runs_after_ctrl_c(cfg, repo, tmp_path, op):
    b = FakeBackend(fail_at=(op, KeyboardInterrupt()))
    assert core.drive(b, _spec(tmp_path), cfg=cfg, repo=repo) == 130
    assert b.calls[-1][0] == "teardown"


def test_teardown_runs_after_an_unexpected_exception(cfg, repo, tmp_path):
    b = FakeBackend(fail_at=("stream", RuntimeError("bug")))
    with pytest.raises(RuntimeError):
        core.drive(b, _spec(tmp_path), cfg=cfg, repo=repo)
    assert b.calls[-1][0] == "teardown"


def test_unverified_teardown_after_a_pass_is_4(cfg, repo, tmp_path, capsys):
    b = FakeBackend(verified=False)
    assert core.drive(b, _spec(tmp_path), cfg=cfg, repo=repo) == 4
    (run_dir,) = (tmp_path / "results").iterdir()
    assert json.loads((run_dir / "summary.json").read_text())["teardown"] == "unverified"


def test_missing_steps_json_is_3(cfg, repo, tmp_path):
    b = FakeBackend()
    original = b.collect

    def collect(run, workdir):
        control, artifacts = original(run, workdir)
        control.write_bytes(_tar_of({"log.txt": b"x"}))
        return control, artifacts

    b.collect = collect
    assert core.drive(b, _spec(tmp_path), cfg=cfg, repo=repo) == 3


def test_env_repeat_runs_once_per_env_and_the_worst_wins(cfg, repo, tmp_path):
    b = FakeBackend(steps=_steps(0, 0, 0), junit=None)  # --no-test: install, configure, build
    runs = []
    original = b.collect

    def collect(run, workdir):
        runs.append(run.run_id)
        if run.env == "cuda-13":
            b.steps = _steps(0, 1)  # the build fails in the second environment
        return original(run, workdir)

    b.collect = collect
    code = core.drive(b, _spec(tmp_path, envs=["cuda-12", "cuda-13"], no_test=True), cfg=cfg, repo=repo)
    assert code == 1
    assert [e for op, e in b.calls if op == "start"] == ["cuda-12", "cuda-13"]
    assert len(set(runs)) == 2  # each environment is its own run with its own ID
    assert len(list((tmp_path / "results").iterdir())) == 2


def test_stale_lock_is_exit_2_before_anything_is_created(cfg, git_repo, tmp_path, monkeypatch):
    def stale(root):
        raise UsageError("error: pixi.lock is out of date")

    monkeypatch.setattr(core, "check_lock", stale)
    b = FakeBackend()
    with pytest.raises(UsageError):
        core.drive(b, _spec(tmp_path), cfg=cfg, repo=git_repo)
    assert b.calls == []


def test_check_lock_uses_a_dry_run(monkeypatch, tmp_path):
    seen = []

    class R:
        returncode = 1
        stdout = ""
        stderr = "lock-file not up-to-date"

    monkeypatch.setattr(core.proc, "run", lambda cmd, **kw: seen.append(cmd) or R())
    with pytest.raises(UsageError) as e:
        core.check_lock(tmp_path)
    assert seen == [["pixi", "lock", "--check", "--dry-run"]]
    assert "fix: pixi lock" in e.value.message


@pytest.fixture
def fake_ref(monkeypatch):
    """--ref runs archive the working tree here, but report a fetched SHA."""
    real = core.tarball.build

    def build(root, ref=None, out_dir=None):
        tb = real(root, out_dir=out_dir)
        tb.ref_sha = "abc1234" + "0" * 33
        return tb

    monkeypatch.setattr(core.tarball, "build", build)


def test_ref_runs_skip_the_host_lock_check_and_record_the_sha(
    cfg, git_repo, tmp_path, monkeypatch, fake_ref
):
    called = []
    monkeypatch.setattr(core, "check_lock", lambda root: called.append(root))
    assert core.drive(FakeBackend(), _spec(tmp_path, ref="abc1234"), cfg=cfg, repo=git_repo) == 0
    assert called == []
    (run_dir,) = (tmp_path / "results").iterdir()
    assert json.loads((run_dir / "summary.json").read_text())["git_sha"] == "abc123400000"  # 12 characters


def test_contributor_code_is_recorded(cfg, git_repo, tmp_path, fake_ref):
    assert core.drive(FakeBackend(), _spec(tmp_path, ref="pr/12"), cfg=cfg, repo=git_repo) == 0
    (run_dir,) = (tmp_path / "results").iterdir()
    s = json.loads((run_dir / "summary.json").read_text())
    assert (s["code"], s["pr"]) == ("contributor", 12)
