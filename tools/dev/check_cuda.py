#!/usr/bin/env python3
"""Compile the CUDA code on a machine without CUDA (RFC-0001 §1.4).

    pixi run check-cuda            # both toolkits: cuda-12 (GCC 11) and cuda-13 (GCC 14)
    pixi run check-cuda cuda-13    # one environment

Runs a linux/arm64 container (native on Apple silicon) with podman or docker, copies
the working tree into it (tracked and untracked, not ignored, files), and builds the
release preset with nvcc. No GPU is needed: this is compile-only. Run it before opening
a pull request that changes CUDA code.
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.dev._paths import repo_root

IMAGE = "ghcr.io/prefix-dev/pixi:latest"
SCRIPT = r"""
set -e
command -v git >/dev/null || (apt-get update -qq && apt-get install -y -qq git >/dev/null)
git config --global --add safe.directory '*'
mkdir -p /w && cd /src
git ls-files -co --exclude-standard -z | tar --null -T - -cf - | tar -x -C /w
cd /w
for env in {envs}; do
  echo "== $env: configure and build (release preset, compile-only)"
  pixi run -e "$env" cmake --preset release -DOSTIA_BUILD_BENCH=ON
  pixi run -e "$env" cmake --build --preset release
  grep -E '^(cuda|cuda_toolkit|architectures|compiler):' "build/$env/release/ostia-summary.txt"
done
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("envs", nargs="*", default=["cuda-12", "cuda-13"])
    args = parser.parse_args(argv)
    engine = shutil.which("podman") or shutil.which("docker")
    if not engine:
        print(
            "error: check-cuda needs podman or docker\n"
            "  fix: install podman (brew install podman && podman machine init && "
            "podman machine start) or Docker Desktop\n"
            "  see: RFC-0001 §1.4",
            file=sys.stderr,
        )
        return 1
    cmd = [
        engine,
        "run",
        "--rm",
        "--platform",
        "linux/arm64",
        "-v",
        f"{repo_root()}:/src:ro",
        "-v",
        "ostia-pixi-cache:/root/.cache/rattler",
        IMAGE,
        "bash",
        "-c",
        SCRIPT.format(envs=" ".join(args.envs)),
    ]
    return subprocess.run(cmd).returncode


if __name__ == "__main__":
    sys.exit(main())
