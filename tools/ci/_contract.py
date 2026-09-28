"""The error-message contract shared by Ostia's tools (RFC-0001, Failure handling).

Every message names the problem, the offending item, the rule that was broken, the exact
fix and the RFC section, in the same shape as cmake/OstiaMessages.cmake's ostia_fail().
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def violation(problem: str, details: list[str], rule: str, fix: str, see: str) -> str:
    lines = [f"error: {problem}"]
    lines += [f"  {d}" for d in details]
    lines += [f"  rule: {rule}", f"  fix: {fix}", f"  see: {see}"]
    return "\n".join(lines)


def load_layering(path: Path | None = None) -> dict:
    """cmake/layering.json: the single source of the dependency table (RFC-0001 §3.3)."""
    return json.loads((path or ROOT / "cmake" / "layering.json").read_text())


def allowed_text(layering: dict, component: str) -> str:
    deps = layering["components"][component]["depends"]
    return ", ".join(deps) if deps else "nothing"
