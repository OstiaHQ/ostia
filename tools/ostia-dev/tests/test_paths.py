"""ostia_dev.paths: the checkout and build locations every tool shares (RFC-0001 §3.5)."""

import subprocess
from pathlib import Path

from ostia_dev import paths


def test_root_is_the_checkout():
    top = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert (paths.ROOT / "cmake" / "layering.json").is_file()
    assert Path(top).resolve() == paths.ROOT
    assert paths.repo_root() == paths.ROOT


def test_build_root_is_per_environment(monkeypatch):
    monkeypatch.setenv("PIXI_ENVIRONMENT_NAME", "clang")
    assert paths.build_root(Path("/src")) == Path("/src/build/clang")
