"""The error-message contract and layering helpers, now in ostia_dev (RFC-0005 §2.3)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools" / "ostia-dev" / "src"))

from ostia_dev.ci.layering import allowed_text, load_layering  # noqa: E402
from ostia_dev.contract import violation  # noqa: E402
from ostia_dev.paths import ROOT  # noqa: E402

__all__ = ["ROOT", "allowed_text", "load_layering", "violation"]
