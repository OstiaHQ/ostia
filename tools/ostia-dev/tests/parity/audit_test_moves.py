#!/usr/bin/env python3
"""Check RFC-0005 §2.3's "the existing tests move with their modules and pass with import
changes only" (ruling B8).

    python3 tools/ostia-dev/tests/parity/audit_test_moves.py

Every test file under the old tool directories must show up as a rename, and every line
that changed in it must be an import, a module or path reference, or a rename from
renames.py. Anything else is printed, and the exit code is 1.
"""

import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from renames import rename  # noqa: E402

BASE = "d8739ef8506cea03eb00318a4048bc279959d8dc"
REPO = HERE.parents[3]
OLD_TESTS = ["tools/ci/tests", "tools/bench/tests", "tests/python"]
ALLOWED = re.compile(
    r"^\s*(from \S+ import .*|import \S+.*|\)|[A-Za-z_]+,?|from \.\S* import .*)\s*$"
    r"|ostia_dev[./]|tools/ostia-dev|\"-m\"|parents\[\d\]"
)


def git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(REPO), *args], capture_output=True, text=True, check=True
    ).stdout


def main() -> int:
    status = git("diff", "-M50%", "--name-status", BASE, "--", *OLD_TESTS, "tools/ostia-dev/tests")
    problems = []
    for line in status.splitlines():
        kind, *paths = line.split("\t")
        old = paths[0]
        if not old.startswith(tuple(OLD_TESTS)) or not old.endswith(".py"):
            continue
        if kind == "D" and old != "tests/python/test_check_cuda.py":
            problems.append(f"{old}: deleted, not moved")
            continue
        if not kind.startswith("R"):
            continue
        new = paths[1]
        diff = git("diff", "-M50%", "-U0", BASE, "HEAD", "--", old, new)
        removed = [
            d[1:] for d in diff.splitlines() if d.startswith("-") and not d.startswith("---")
        ]
        added = [d[1:] for d in diff.splitlines() if d.startswith("+") and not d.startswith("+++")]
        renamed = {rename(r) for r in removed}
        for a in added:
            if a in renamed or ALLOWED.search(a):
                continue
            problems.append(f"{new}: + {a}")
    for p in problems:
        print(p)
    print(f"audit: {len(problems)} line(s) beyond imports, paths and renames")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
