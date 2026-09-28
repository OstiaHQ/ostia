"""Paths shared by the development tools (RFC-0001 §3.5).

Every tool resolves the repository from its own location, so it works from any cwd.
"""

import os
from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def env_name() -> str:
    return os.environ.get("PIXI_ENVIRONMENT_NAME", "default")


def build_root(source_root: Path) -> Path:
    """One build tree per pixi environment: <source>/build/<env> (RFC-0001 §3.5)."""
    return source_root / "build" / env_name()


def conda_prefix() -> Path | None:
    prefix = os.environ.get("CONDA_PREFIX")
    return Path(prefix) if prefix else None
