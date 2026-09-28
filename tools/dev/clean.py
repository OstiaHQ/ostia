#!/usr/bin/env python3
"""Remove build output and everything `pixi run py-dev` installed (RFC-0001 §3.5).

    pixi run clean

Removes the files listed in each install manifest from the pixi environment, uninstalls
the ostia-* editables that point at this checkout, and deletes build/<env>. The CPM
source cache (.cache/cpm) is kept, so the next build does not download again.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.dev._paths import build_root, repo_root


def main() -> int:
    root = repo_root()
    build = build_root(root)
    removed = 0
    for manifest in build.glob("*/install_manifest.txt"):
        for line in manifest.read_text().splitlines():
            path = Path(line)
            if path.is_file() or path.is_symlink():
                path.unlink()
                removed += 1
    print(f"removed {removed} installed files")
    layering = json.loads((root / "cmake" / "layering.json").read_text())
    dists = [
        f"ostia-{c}"
        for c in layering["components"]
        if (root / c / "python" / "pyproject.toml").exists()
    ]
    subprocess.run([sys.executable, "-m", "pip", "uninstall", "-y", "--quiet", *dists], check=False)
    print(f"uninstalled {', '.join(dists)}")
    if build.exists():
        shutil.rmtree(build)
        print(f"removed {build}")
    print("kept .cache/cpm (the pinned source cache)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
