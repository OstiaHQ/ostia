"""Suites become supervisor step plans (RFC-0005 §3.2, §3.3; R5, R6, R16)."""

import os
import shlex
from pathlib import Path

import pytest
from ostia_dev import config
from ostia_dev.errors import UsageError
from ostia_dev.remote import profiles, suites

GOLDEN = Path(__file__).parent / "golden" / "suites"


@pytest.fixture
def cfg(tmp_path):
    return config.load(tmp_path / "none.toml")


def _golden(name: str, text: str) -> None:
    path = GOLDEN / f"{name}.txt"
    if os.environ.get("OSTIA_UPDATE_GOLDEN"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    assert text == path.read_text(), f"golden {path.name} differs; OSTIA_UPDATE_GOLDEN=1 updates it"


@pytest.mark.parametrize("suite", ["gpu", "sanitizer", "bench-smoke", "overhead-aa", "cpu"])
def test_golden_suite_plans_on_l4_cuda_12(cfg, suite):
    p = profiles.resolve("l4", "gke", cfg)
    plan = suites.build_plan(cfg, p, "cuda-12", suite=suite, run_id="k8s-l4-20261002-141501-a1b2c3")
    _golden(f"{suite}-l4-cuda-12", suites.encode(plan.steps) + "\n")


def test_golden_cpu_suite_on_the_cpu_profile(cfg):
    p = profiles.resolve("cpu", None, cfg)
    plan = suites.build_plan(cfg, p, "default", suite="cpu", run_id="container-cpu-x")
    _golden("cpu-cpu-default", suites.encode(plan.steps) + "\n")


def test_install_is_locked_and_later_steps_are_frozen(cfg):
    plan = suites.build_plan(cfg, profiles.resolve("cpu", None, cfg), "default", suite="cpu")
    by_kind = {s.kind: s for s in plan.steps}
    assert by_kind["install"].argv == ("pixi", "install", "--locked", "-e", "default")
    for s in plan.steps:
        if s.kind in ("build", "command", "report"):
            assert s.argv[:5] == ("pixi", "run", "--frozen", "-e", "default")


def test_gpu_profiles_get_the_preflight_steps(cfg):
    gpu = suites.build_plan(cfg, profiles.resolve("l4", "gke", cfg), "cuda-12", suite="cpu")
    cpu = suites.build_plan(cfg, profiles.resolve("cpu", None, cfg), "default", suite="cpu")
    assert [s.name for s in gpu.steps if s.kind == "preflight"] == [
        "gpu-preflight",
        "compute-capability",
        "libcuda",
    ]
    assert not [s for s in cpu.steps if s.kind == "preflight"]
    fingerprint = gpu.steps[0]
    assert "tools/ci/gpu_preflight.sh" in fingerprint.argv[-1]
    assert ".ostia/fingerprint.txt" in fingerprint.argv[-1]
    assert "8.9" in gpu.steps[1].argv[-1]


def test_suite_metadata(cfg):
    p = profiles.resolve("l4", "gke", cfg)
    plan = suites.build_plan(cfg, p, "cuda-12", suite="gpu")
    assert plan.preset == "dev"
    assert plan.build_dir == "/w/build/cuda-12/dev"
    assert plan.expect_summary == "architectures: native (dev preset, GPU detected (8.9))"
    assert suites.build_plan(cfg, p, "cuda-12", suite="sanitizer").preset == "level-debug"


def test_default_command_is_ctest_with_junit(cfg):
    p = profiles.resolve("cpu", None, cfg)
    plan = suites.build_plan(cfg, p, "default")
    names = [s.name for s in plan.steps]
    assert names == ["install", "configure", "build", "command"]
    cmd = plan.steps[-1].argv[5:]
    assert cmd == (
        "ctest",
        "--test-dir",
        "/w/build/default/dev",
        "--output-on-failure",
        "-j",
        "4",
        "--output-junit",
        "junit.xml",
    )


def test_custom_ctest_command_gets_junit(cfg):
    p = profiles.resolve("cpu", None, cfg)
    plan = suites.build_plan(cfg, p, "default", command=["ctest", "-L", "gpu"])
    assert plan.steps[-1].argv[5:] == ("ctest", "-L", "gpu", "--output-junit", "junit.xml")
    plan = suites.build_plan(cfg, p, "default", command=["ctest", "--output-junit", "x.xml"])
    assert plan.steps[-1].argv[5:] == ("ctest", "--output-junit", "x.xml")  # already there


def test_other_custom_commands_are_unchanged(cfg):
    p = profiles.resolve("cpu", None, cfg)
    plan = suites.build_plan(cfg, p, "default", command=["python", "-c", "print(1)"])
    assert plan.steps[-1].argv[5:] == ("python", "-c", "print(1)")


def test_non_dev_presets_get_the_profile_architecture_on_gpu_profiles(cfg):
    gpu = profiles.resolve("l4", "gke", cfg)
    cpu = profiles.resolve("cpu", None, cfg)
    configure = lambda plan: next(s for s in plan.steps if s.name == "configure").argv[5:]  # noqa: E731
    assert configure(suites.build_plan(cfg, gpu, "cuda-12", preset="release")) == (
        "cmake",
        "--preset",
        "release",
        "-DCMAKE_CUDA_ARCHITECTURES=89-real",
    )
    assert configure(suites.build_plan(cfg, gpu, "cuda-12")) == ("cmake", "--preset", "dev")
    assert configure(suites.build_plan(cfg, cpu, "cuda-12", preset="release")) == (
        "cmake",
        "--preset",
        "release",
    )


def test_no_build_and_no_test(cfg):
    p = profiles.resolve("cpu", None, cfg)
    no_build = suites.build_plan(cfg, p, "default", no_build=True)
    assert [s.name for s in no_build.steps] == ["install", "command"]
    no_test = suites.build_plan(cfg, p, "default", no_test=True)
    assert [s.name for s in no_test.steps] == ["install", "configure", "build"]
    suite_no_test = suites.build_plan(cfg, p, "default", suite="cpu", no_test=True)
    assert [s.kind for s in suite_no_test.steps] == ["install", "build", "build"]


def test_unknown_suite_is_exit_2(cfg):
    with pytest.raises(UsageError) as e:
        suites.build_plan(cfg, profiles.resolve("cpu", None, cfg), "default", suite="nope")
    assert "bench-smoke, cpu, gpu, overhead-aa, sanitizer" in e.value.message


def test_suite_and_command_together_is_exit_2(cfg):
    with pytest.raises(UsageError):
        suites.build_plan(
            cfg, profiles.resolve("cpu", None, cfg), "default", suite="cpu", command=["true"]
        )


@pytest.mark.parametrize(
    "argv",
    [
        ["python", "-c", "print('a b')"],
        ["sh", "-c", 'echo "$HOME" && echo \\$x'],
        ["echo", "it's", "a", "*", "$(whoami)", "`id`", ";", "|"],
    ],
)
def test_encode_round_trips_through_shlex(argv):
    line = suites.encode([suites.Step("command", "command", tuple(argv))])
    name, kind, command = line.split("\t")
    assert (name, kind) == ("command", "command")
    assert shlex.split(command) == argv


@pytest.mark.parametrize("bad", ["a\nb", "a\tb"])
def test_encode_refuses_newlines_and_tabs(bad):
    with pytest.raises(UsageError) as e:
        suites.encode([suites.Step("command", "command", ("python", "-c", bad))])
    assert e.value.code == 2
    assert "script" in e.value.message


@pytest.mark.parametrize("name", ["Build", "-x", "a b", "a\tb", "", "é"])
def test_step_names_are_checked(name):
    with pytest.raises(ValueError):
        suites.Step(name, "build", ("true",))


def test_step_kinds_are_checked():
    with pytest.raises(ValueError):
        suites.Step("x", "deploy", ("true",))
