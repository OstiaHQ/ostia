"""The container backend against a fake engine (RFC-0005 §5; R3, R4, R14)."""

import json

import pytest
from fakes.engine import FakeEngine
from ostia_dev import config
from ostia_dev.errors import UsageError
from ostia_dev.remote import container, core


@pytest.fixture
def cfg(tmp_path):
    return config.load(tmp_path / "none.toml")


@pytest.fixture
def repo(git_repo, monkeypatch):
    monkeypatch.setattr(core, "check_lock", lambda root: None)
    return git_repo


@pytest.fixture
def engine(tmp_path):
    return FakeEngine(tmp_path / "engine")


def _drive(engine, cfg, repo, tmp_path, *, kind="podman", gpus=False, **spec):
    backend = container.ContainerBackend(kind, gpus=gpus, runner=engine, which=engine.which,
                                         arch="arm64")  # fmt: skip
    base = dict(backend="container", profile="cpu", envs=["default"], results=tmp_path / "res")
    base.update(spec)
    return core.drive(backend, core.RunSpec(**base), cfg=cfg, repo=repo)


def _subs(engine):
    return [c[1] if c[1] != "exec" else "exec:" + c[c.index("--user") + 3] for c in engine.calls]


def test_podman_argv(engine, cfg, repo, tmp_path):
    assert _drive(engine, cfg, repo, tmp_path) == 0
    argv = engine.container()["argv"]
    assert argv[:4] == ["/usr/bin/podman", "run", "-d", "--name"]
    for flag in (["--init"], ["--platform", "linux/arm64"], ["--user", "1000:1000"],
                 ["--cap-drop", "ALL"], ["--security-opt", "no-new-privileges"],
                 ["-v", "/w"], ["-v", "ostia-pixi-cache:/w/cache/rattler:U"]):  # fmt: skip
        assert any(argv[i : i + len(flag)] == flag for i in range(len(argv))), flag
    assert "--cap-add" not in argv
    labels = [argv[i + 1] for i, a in enumerate(argv) if a == "--label"]
    assert "ostia.dev/managed=true" in labels
    assert any(lbl.startswith("ostia.dev/run-id=container-cpu-") for lbl in labels)
    assert any(lbl.startswith("ostia.dev/expires=") for lbl in labels)
    assert any(lbl.startswith("ostia.dev/owner=") for lbl in labels)
    image = argv.index(cfg.image)
    assert argv[image + 1 : image + 3] == ["/bin/sh", "-c"]
    assert argv[image + 3].startswith("# ostia-supervisor")
    assert argv[image + 4] == "ostia-supervisor"
    env = engine.container()["env"]
    assert env["HOME"] == "/w/home" and env["OSTIA_ENV"] == "default"
    assert env["RATTLER_CACHE_DIR"] == "/w/cache/rattler"  # outside HOME, which stays uid 1000's
    assert env["OSTIA_PLAN"].splitlines()[0] == "install\tinstall\tpixi install --locked -e default"
    assert "OSTIA_REQUIRE_GPU" not in env
    assert env["CTEST_NO_TESTS_ACTION"] == "error"


def test_docker_argv_uses_the_root_init_step(engine, cfg, repo, tmp_path):
    assert _drive(engine, cfg, repo, tmp_path, kind="docker") == 0
    argv = engine.container()["argv"]
    assert "--user" not in argv[: argv.index(cfg.image)]
    caps = [argv[i + 1] for i, a in enumerate(argv) if a == "--cap-add"]
    assert caps == ["CHOWN", "SETUID", "SETGID"]
    assert argv[argv.index("ostia-pixi-cache:/w/cache/rattler") - 1] == "-v"
    image = argv.index(cfg.image)
    assert argv[image + 1 : image + 4] == ["/bin/sh", "-c", container.ROOT_INIT]
    assert argv[image + 4] == "ostia-init" and argv[image + 5].startswith("# ostia-supervisor")
    assert "setpriv --reuid=1000 --regid=1000" in container.ROOT_INIT
    assert "chown 1000:1000 /w /w/cache /w/cache/rattler" in container.ROOT_INIT


@pytest.mark.parametrize(("kind", "flags"), [("podman", ["--device", "nvidia.com/gpu=all"]),
                                             ("docker", ["--gpus", "all"])])  # fmt: skip
def test_gpus_per_engine(engine, cfg, repo, tmp_path, kind, flags):
    code = _drive(engine, cfg, repo, tmp_path, kind=kind, gpus=True, profile="l4",
                  envs=["cuda-12"], no_test=True)  # fmt: skip
    assert code == 0
    argv = engine.container()["argv"]
    i = argv.index(flags[0])
    assert argv[i : i + 2] == flags
    env = engine.container()["env"]
    assert env["OSTIA_REQUIRE_GPU"] == "1"
    assert env["OSTIA_PLAN"].splitlines()[0].startswith("gpu-preflight\tpreflight\t")


def test_gpus_without_a_gpu_profile_is_exit_2(engine, cfg, repo, tmp_path):
    with pytest.raises(UsageError) as e:
        _drive(engine, cfg, repo, tmp_path, gpus=True)
    assert "--profile" in e.value.message
    assert engine.calls == []


def test_a_gpu_profile_without_gpus_is_exit_2(engine, cfg, repo, tmp_path):
    with pytest.raises(UsageError):
        _drive(engine, cfg, repo, tmp_path, profile="l4", envs=["cuda-12"])


def test_the_operations_in_order(engine, cfg, repo, tmp_path):
    assert _drive(engine, cfg, repo, tmp_path) == 0
    assert _subs(engine) == [
        "ps", "inspect", "run", "exec:tar", "exec:sh", "exec:touch", "logs", "inspect",
        "exec:sh", "exec:sh", "exec:touch", "rm", "inspect",
    ]  # fmt: skip
    touched = sorted(p.name for p in (tmp_path / "engine").rglob(".ostia/*"))
    assert touched == ["collected", "ready"]


def test_results_layout(engine, cfg, repo, tmp_path):
    assert _drive(engine, cfg, repo, tmp_path) == 0
    (run_dir,) = (tmp_path / "res").iterdir()
    assert run_dir.name.startswith("container-cpu-")
    for f in ("summary.json", "log.txt", "junit.xml", "ostia-summary.txt"):
        assert (run_dir / f).exists(), f
    s = json.loads((run_dir / "summary.json").read_text())
    assert (s["backend"], s["engine"], s["teardown"], s["exit_code"]) == (
        "container", "podman", "verified", 0,
    )  # fmt: skip


def test_teardown_on_test_failure(engine, cfg, repo, tmp_path):
    engine.step_codes = {"command": 8}
    engine.junit = "failed.xml"
    assert _drive(engine, cfg, repo, tmp_path) == 1
    assert engine.container()["removed"]


def test_teardown_on_an_exception(engine, cfg, repo, tmp_path):
    engine.raise_in["stream"] = RuntimeError("bug")
    with pytest.raises(RuntimeError):
        _drive(engine, cfg, repo, tmp_path)
    assert engine.container()["removed"]


def test_teardown_on_ctrl_c(engine, cfg, repo, tmp_path):
    engine.raise_in["stream"] = KeyboardInterrupt()
    assert _drive(engine, cfg, repo, tmp_path) == 130
    assert engine.container()["removed"]


def test_unverified_teardown_is_4(engine, cfg, repo, tmp_path, monkeypatch):
    monkeypatch.setattr(container.time, "sleep", lambda s: None)
    engine.keep_after_rm = True
    assert _drive(engine, cfg, repo, tmp_path) == 4


@pytest.mark.parametrize("fail", ["tar-x", "count"])
def test_failed_upload_is_exit_3_and_teardown(engine, cfg, repo, tmp_path, fail, capsys):
    engine.fail[fail] = 1
    assert _drive(engine, cfg, repo, tmp_path) == 3
    assert engine.container()["removed"]
    assert "exec:touch" not in _subs(engine)  # no ready marker
    (run_dir,) = (tmp_path / "res").iterdir()
    assert json.loads((run_dir / "summary.json").read_text())["failing_step"] == "upload"
    assert "ephemeral_storage" in capsys.readouterr().err


def test_failed_start_is_exit_3(engine, cfg, repo, tmp_path):
    engine.fail["run"] = 125
    assert _drive(engine, cfg, repo, tmp_path) == 3


def test_oom_kill_is_exit_3(engine, cfg, repo, tmp_path):
    engine.oom = True
    engine.running_after_stream = False
    engine.step_codes = {"build": 137}
    assert _drive(engine, cfg, repo, tmp_path) == 3
    assert "cp" in _subs(engine)  # a stopped container is read with cp


def test_env_repeat_gives_two_runs_and_the_worst_wins(engine, cfg, repo, tmp_path):
    runs = []
    real_run = engine.run

    def run(cmd, **kw):
        r = real_run(cmd, **kw)
        if cmd[1] == "run":
            runs.append(cmd[cmd.index("--name") + 1])
            if len(runs) == 2:
                engine.step_codes = {"build": 2}
        return r

    engine.run = run
    code = _drive(engine, cfg, repo, tmp_path, envs=["cuda-12", "cuda-13"], preset="release",
                  no_test=True)  # fmt: skip
    assert code == 1
    assert len(runs) == 2 and runs[0] != runs[1]
    envs = [c["env"]["OSTIA_ENV"] for c in engine.containers.values()]
    assert envs == ["cuda-12", "cuda-13"]


def test_no_build_and_no_test_reach_the_plan(engine, cfg, repo, tmp_path):
    assert _drive(engine, cfg, repo, tmp_path, no_build=True, command=["python", "-c", "1"]) == 0
    plan = engine.container()["env"]["OSTIA_PLAN"].splitlines()
    assert [line.split("\t")[0] for line in plan] == ["install", "command"]


def test_gc_removes_expired_containers_only(engine, cfg, repo, tmp_path):
    engine.expired = ["ostia-old"]
    assert _drive(engine, cfg, repo, tmp_path) == 0
    removed = [c[-1] for c in engine.calls if c[1] == "rm"]
    assert removed[0] == "ostia-old" and "ostia-fresh" not in removed


def test_no_engine_is_exit_2():
    with pytest.raises(UsageError) as e:
        container.detect_engine(None, which=lambda n: None)
    assert "brew install podman" in e.value.message and e.value.code == 2


def test_a_requested_engine_that_is_missing_is_exit_2():
    with pytest.raises(UsageError) as e:
        container.detect_engine("docker", which=lambda n: "/x/podman" if n == "podman" else None)
    assert "docker" in e.value.message


def test_podman_is_preferred():
    assert container.detect_engine(None, which=lambda n: f"/x/{n}") == "/x/podman"
