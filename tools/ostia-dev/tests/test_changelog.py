"""CHANGELOG.md lists every old name with its replacement (RFC-0005 §2.3, ruling B12)."""

from ostia_dev.paths import ROOT
from renames import SCRIPTS, TASKS


def _section(title: str) -> str:
    text = (ROOT / "CHANGELOG.md").read_text().split("## [Unreleased]", 1)[1].split("\n## ", 1)[0]
    return text.split(f"### {title}\n", 1)[1].split("\n### ", 1)[0]


def test_changelog_lists_every_rename():
    rows = [r for r in _section("Changed").splitlines() if r.startswith("| `")]
    missing = []
    for task, new in TASKS.items():
        if not any(f"`pixi run {task}" in r and f"ostia-dev {' '.join(new)}" in r for r in rows):
            missing.append(task)
    for script, (new, _) in SCRIPTS.items():
        if new and not any(script in r and f"ostia-dev {' '.join(new)}".strip() in r for r in rows):
            missing.append(script)
    assert missing == []


def test_changelog_lists_removals():
    removed = _section("Removed")
    assert "`gpu-ci`" in removed and "check-cuda" in removed
