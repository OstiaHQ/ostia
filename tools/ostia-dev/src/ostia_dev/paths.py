"""Paths shared by the development tools (RFC-0001 §3.5).

Every tool resolves the repository from its own location, so it works from any cwd.
"""

import os
from pathlib import Path

# ostia-dev is installed editable (RFC-0005 §1.1), so this file sits in the checkout.
ROOT = Path(__file__).resolve().parents[4]


def repo_root() -> Path:
    return ROOT


def env_name() -> str:
    return os.environ.get("PIXI_ENVIRONMENT_NAME", "default")


def build_root(source_root: Path) -> Path:
    """One build tree per pixi environment: <source>/build/<env> (RFC-0001 §3.5)."""
    return source_root / "build" / env_name()


def conda_prefix() -> Path | None:
    prefix = os.environ.get("CONDA_PREFIX")
    return Path(prefix) if prefix else None
