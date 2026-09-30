"""Shared fixtures for the ostia-dev tests."""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture(autouse=True)
def _isolated_git(tmp_path_factory, monkeypatch):
    """No global or system git config (signing, hooks, templates) leaks into the tests."""
    home = tmp_path_factory.mktemp("gitconfig")
    cfg = home / "config"
    cfg.write_text(
        "[user]\n\tname = Test\n\temail = test@example.com\n[init]\n\tdefaultBranch = main\n"
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


@pytest.fixture
def git_repo(tmp_path) -> Path:
    """A committed repository with the real .gitignore of this checkout."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    shutil.copy(REPO / ".gitignore", repo / ".gitignore")
    (repo / "README.md").write_text("hello\n")
    (repo / "src").mkdir()
    (repo / "src" / "a.cpp").write_text("int a;\n")
    tool = repo / "tools" / "run.sh"
    tool.parent.mkdir()
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "initial")
    return repo


@pytest.fixture
def git_worktree(git_repo, tmp_path) -> Path:
    """A linked worktree of git_repo: its .git is a `gitdir:` file."""
    wt = tmp_path / "wt"
    git(git_repo, "worktree", "add", "-q", "-b", "wt-branch", str(wt))
    return wt
