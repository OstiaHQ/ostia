#!/usr/bin/env python3
"""Remove build output and everything `pixi run py-dev` installed (RFC-0001 §3.5).

    pixi run clean

Removes the files install-native recorded (build/<env>/native-install-manifest.txt, a
copy that ctest's staging install cannot overwrite) from the pixi environment, uninstalls
the ostia-* editables that point at this checkout, and deletes build/<env>. The CPM
source cache (.cache/cpm) is kept, so the next build does not download again.
"""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.dev._paths import build_root, repo_root

NATIVE_MANIFEST = "native-install-manifest.txt"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--build-root", type=Path, help="default: build/<env>")
    parser.add_argument("--skip-pip", action="store_true", help="leave the editables (tests)")
    args = parser.parse_args(argv)
    root = repo_root()
    build = args.build_root or build_root(root)
    removed = 0
    manifest = build / NATIVE_MANIFEST
    if manifest.exists():
        for line in manifest.read_text().splitlines():
            path = Path(line)
            if path.is_file() or path.is_symlink():
                path.unlink()
                removed += 1
    print(f"removed {removed} installed files")
    if not args.skip_pip:
        layering = json.loads((root / "cmake" / "layering.json").read_text())
        dists = [
            f"ostia-{c}"
            for c in layering["components"]
            if (root / c / "python" / "pyproject.toml").exists()
        ]
        subprocess.run(
            [sys.executable, "-m", "pip", "uninstall", "-y", "--quiet", *dists], check=False
        )
        print(f"uninstalled {', '.join(dists)}")
    if build.exists():
        shutil.rmtree(build)
        print(f"removed {build}")
    print("kept .cache/cpm (the pinned source cache)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
