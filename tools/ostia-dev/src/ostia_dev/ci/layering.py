"""cmake/layering.json, the single source of the dependency table (RFC-0001 §3.3)."""

import json
from pathlib import Path

from ostia_dev.paths import ROOT


def load_layering(path: Path | None = None) -> dict:
    return json.loads((path or ROOT / "cmake" / "layering.json").read_text())


def allowed_text(layering: dict, component: str) -> str:
    deps = layering["components"][component]["depends"]
    return ", ".join(deps) if deps else "nothing"
