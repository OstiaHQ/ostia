"""Run one pre-migration tool from the base worktree, as `python <script> ARGS` did.

    python run_old.py BASE [--repo-root REPO] SCRIPT [ARGS...]

With --repo-root, the old tools.dev._paths.repo_root() answers REPO instead of BASE, so a
tool that inspects the checkout (doctor) looks at the same tree as its replacement.
"""

import runpy
import sys
from pathlib import Path

base = Path(sys.argv[1])
rest = sys.argv[2:]
sys.path.insert(0, str(base))
if rest[0] == "--repo-root":
    repo = Path(rest[1])
    rest = rest[2:]
    import tools.dev._paths as paths

    paths.repo_root = lambda: repo
script = str(base / rest[0])
sys.argv = [script, *rest[1:]]
runpy.run_path(script, run_name="__main__")
