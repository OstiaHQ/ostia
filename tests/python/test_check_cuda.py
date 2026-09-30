"""The check-cuda regression contract (R9): what it keeps doing, and what its worktree fix
changes on purpose (ADR-0014 follow-up; RFC-0005 §4.10's host-side tarball)."""

import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from tools.dev import check_cuda

DIGEST = "ghcr.io/prefix-dev/pixi:0.81.0-noble@sha256:"


def _which(found: set[str]):
    return lambda name: f"/usr/bin/{name}" if name in found else None


def test_default_envs_are_both_toolkits():
    argv, script = check_cuda.build_command(["cuda-12", "cuda-13"], "/usr/bin/podman")
    assert "for env in cuda-12 cuda-13; do" in script
    assert check_cuda.parse_args([]).envs == ["cuda-12", "cuda-13"]
    assert check_cuda.parse_args(["cuda-13"]).envs == ["cuda-13"]


def test_podman_is_preferred(monkeypatch):
    monkeypatch.setattr(shutil, "which", _which({"podman", "docker"}))
    assert check_cuda.find_engine() == "/usr/bin/podman"
    monkeypatch.setattr(shutil, "which", _which({"docker"}))
    assert check_cuda.find_engine() == "/usr/bin/docker"


def test_no_engine_is_exit_1_with_the_same_fix(monkeypatch, capsys):
    monkeypatch.setattr(shutil, "which", _which(set()))
    assert check_cuda.main([]) == 1
    err = capsys.readouterr().err
    assert err == (
        "error: check-cuda needs podman or docker\n"
        "  fix: install podman (brew install podman && podman machine init && "
        "podman machine start) or Docker Desktop\n"
        "  see: RFC-0001 §1.4\n"
    )


def test_container_arguments():
    argv, script = check_cuda.build_command(["cuda-12"], "/usr/bin/podman")
    assert argv[:4] == ["/usr/bin/podman", "run", "--rm", "-i"]  # -i: the tar comes on stdin
    i = argv.index("--platform")
    assert argv[i + 1] == "linux/arm64"
    assert argv[argv.index("ostia-pixi-cache:/root/.cache/rattler") - 1] == "-v"
    assert not any(a.endswith(":/src:ro") for a in argv)
    image = next(a for a in argv if a.startswith("ghcr.io/prefix-dev/pixi"))
    assert image.startswith(DIGEST)  # pinned, was :latest
    from ostia_dev import config

    assert image == config.builtin_defaults()["image"]
    assert argv[argv.index(image) + 1 :] == ["sh", "-c", script]


def test_script_builds_each_env_and_prints_the_summary():
    _, script = check_cuda.build_command(["cuda-12", "cuda-13"], "/usr/bin/docker")
    assert script.lstrip().startswith("set -eu")
    assert "tar -x -C /w" in script
    assert 'pixi run -e "$env" cmake --preset release -DOSTIA_BUILD_BENCH=ON' in script
    assert 'pixi run -e "$env" cmake --build --preset release' in script
    assert (
        "grep -E '^(cuda|cuda_toolkit|architectures|compiler):' "
        '"build/$env/release/ostia-summary.txt"'
    ) in script
    assert "git" not in script and "apt-get" not in script


def test_main_returns_the_containers_exit_code(monkeypatch, tmp_path):
    monkeypatch.setattr(shutil, "which", _which({"podman"}))
    fake_tar = tmp_path / "upload.tar"
    with tarfile.open(fake_tar, "w"):
        pass

    class TB:
        path = fake_tar

    monkeypatch.setattr(check_cuda.tarball, "build", lambda root, out_dir=None: TB())
    seen = {}

    def run(argv, stdin=None):
        seen.update(argv=argv, stdin=stdin.name)
        return subprocess.CompletedProcess(argv, 7)

    monkeypatch.setattr(check_cuda.subprocess, "run", run)
    assert check_cuda.main(["cuda-12"]) == 7
    assert seen["stdin"] == str(fake_tar)


@pytest.fixture
def worktree(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    (tmp_path / "gitconfig").write_text("[user]\n\tname = T\n\temail = t@example.com\n")

    def git(*args, cwd):
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)

    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "-q", cwd=repo)
    (repo / "CMakePresets.json").write_text("{}\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "x", cwd=repo)
    wt = tmp_path / "wt"
    git("worktree", "add", "-q", str(wt), cwd=repo)
    (wt / "kernel.cu").write_text("__global__ void k() {}\n")
    return wt


def test_the_host_tar_of_a_worktree_holds_its_files(worktree, tmp_path):
    assert (worktree / ".git").is_file()  # the case that broke: .git is a gitdir: file
    tb = check_cuda.tarball.build(Path(worktree), out_dir=tmp_path / "out")
    with tarfile.open(tb.path) as t:
        assert sorted(t.getnames()) == ["CMakePresets.json", "kernel.cu"]
