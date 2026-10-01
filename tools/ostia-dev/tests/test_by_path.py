"""Four ci modules run by path with plain python3, before pixi exists (ruling B6): CMake's
ctest in the no-pixi container jobs, the fresh-runner docs-as-test job and the Claude Code
hook. -S drops site-packages, so typer and PyYAML can't sneak in."""

import json
import subprocess
import sys

import pytest
from ostia_dev.paths import ROOT

CI = ROOT / "tools" / "ostia-dev" / "src" / "ostia_dev" / "ci"


@pytest.mark.parametrize("name", ["check_exports", "check_graph", "docs_as_test", "check_comments"])
def test_runs_without_site_packages(name):
    r = subprocess.run(
        [sys.executable, "-S", "-I", str(CI / f"{name}.py"), "--help"],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr


def test_hook_command_points_at_an_existing_file():
    settings = json.loads((ROOT / ".claude" / "settings.json").read_text())
    [entry] = settings["hooks"]["PostToolUse"]
    command = entry["hooks"][0]["command"]
    path = command.split('"')[1].replace("$CLAUDE_PROJECT_DIR", str(ROOT))
    assert path.endswith("ostia_dev/ci/check_comments.py") and (ROOT / path).is_file()
