"""PR B parity (RFC-0005 §2.3, Testing "PR B parity"): every old entry point against its
ostia-dev replacement, on the same fixtures.

The old tools come from a detached worktree of the commit PR B started from, so this
suite still runs after the branch has deleted them.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from renames import rename

REPO = Path(__file__).resolve().parents[4]
BASE = "d8739ef8506cea03eb00318a4048bc279959d8dc"
RUN_OLD = Path(__file__).with_name("run_old.py")
# Values that differ between any two runs, whatever the tool's name.
VOLATILE = [(re.compile(r'"date": "[0-9T:\-]+Z"'), '"date": "<date>"')]


@pytest.fixture(scope="session")
def base_tree(tmp_path_factory) -> Path:
    have = subprocess.run(["git", "-C", str(REPO), "cat-file", "-e", f"{BASE}^{{commit}}"])
    if have.returncode != 0:
        pytest.skip(f"the base commit is missing; fix: git fetch origin {BASE}")
    tree = tmp_path_factory.mktemp("parity") / "base"
    subprocess.run(
        ["git", "-C", str(REPO), "worktree", "add", "-q", "--detach", str(tree), BASE],
        check=True,
    )
    yield tree
    subprocess.run(["git", "-C", str(REPO), "worktree", "remove", "--force", str(tree)])


def env(**extra: str) -> dict[str, str]:
    """The same environment for both sides; the commit SHA is pinned because the two sides
    run from different checkouts."""
    return dict(os.environ, OSTIA_GIT_SHA="parity", **extra)


def run_old(
    base: Path,
    script: str,
    args: list[str],
    *,
    cwd: Path,
    stdin: str | None = None,
    repo_root: bool = False,
    interpreter: str | None = None,
) -> subprocess.CompletedProcess:
    if interpreter:
        cmd = [interpreter, str(base / script), *args]
    else:
        root = ["--repo-root", str(REPO)] if repo_root else []
        cmd = [sys.executable, str(RUN_OLD), str(base), *root, script, *args]
    return subprocess.run(cmd, cwd=cwd, input=stdin, capture_output=True, text=True, env=env())


def run_new(args: list[str], *, cwd: Path, stdin: str | None = None) -> subprocess.CompletedProcess:
    exe = shutil.which("ostia-dev")
    assert exe, "ostia-dev is not on PATH: run the suite through pixi"
    return subprocess.run(
        [exe, *args], cwd=cwd, input=stdin, capture_output=True, text=True, env=env()
    )


def normalise(text: str, swaps: list[tuple[str, str]]) -> str:
    """Old output in the new vocabulary: the old side's paths, then every rename."""
    for old, new in swaps:
        text = text.replace(old, new)
    text = rename(text)
    for pattern, sub in VOLATILE:
        text = pattern.sub(sub, text)
    return text


def snapshot(root: Path, swaps: list[tuple[str, str]] | None = None) -> dict[str, str]:
    """Every file under root (text, normalised when swaps are given), keyed by its path."""
    files = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and ".git" not in p.parts:
            text = p.read_bytes().decode("utf-8", "replace")
            files[str(p.relative_to(root))] = (
                normalise(text, swaps) if swaps is not None else _volatile(text)
            )
    return files


def _volatile(text: str) -> str:
    for pattern, sub in VOLATILE:
        text = pattern.sub(sub, text)
    return text
