"""No old pixi task name or tool path comes back after the hard switch (RFC-0005 §2.3,
ruling B15). Accepted design documents and the CHANGELOG keep them as history."""

import re
import subprocess

from ostia_dev.paths import ROOT
from renames import SCRIPTS, TASKS, task_pattern

EXEMPT = (
    "CHANGELOG.md",
    "docs/rfcs/",
    "docs/adr/",
    "pixi.lock",
    "tools/ostia-dev/tests/renames.py",
    "tools/ostia-dev/tests/test_old_names.py",
    # schema 1's $id names its first home; it identifies the schema, not a file (ruling B17).
    "tools/ostia-dev/src/ostia_dev/bench/schema-v1.json",
)
PATTERNS = [task_pattern(t) for t in TASKS] + [
    re.compile(r"(?<![\w/-])" + re.escape(s) + r"(?![\w-])") for s in SCRIPTS
]


def old_names(root=ROOT) -> list[str]:
    listed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True
    ).stdout.decode()
    found = []
    for rel in filter(None, listed.split("\0")):
        path = root / rel
        if rel.startswith(EXEMPT) or not path.is_file():
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for p in PATTERNS:
                if m := p.search(line):
                    found.append(f"{rel}:{n}: {m.group(0)}")
    return found


def test_no_old_names_in_tracked_files():
    assert old_names() == []
