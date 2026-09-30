#!/usr/bin/env python3
"""Compile the CUDA code on a machine without CUDA (RFC-0001 §1.4).

    pixi run check-cuda            # both toolkits: cuda-12 (GCC 11) and cuda-13 (GCC 14)
    pixi run check-cuda cuda-13    # one environment

Runs a linux/arm64 container (native on Apple silicon) with podman or docker, streams the
working tree into it as a tar built on the host, and builds the release preset with nvcc.
No GPU is needed: this is compile-only. Run it before opening a pull request that changes
CUDA code.
"""

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ostia_dev import config
from ostia_dev.remote import tarball

from tools.dev._paths import repo_root

SCRIPT = r"""set -eu
mkdir -p /w
tar -x -C /w
cd /w
for env in {envs}; do
  echo "== $env: configure and build (release preset, compile-only)"
  pixi run -e "$env" cmake --preset release -DOSTIA_BUILD_BENCH=ON
  pixi run -e "$env" cmake --build --preset release
  grep -E '^(cuda|cuda_toolkit|architectures|compiler):' "build/$env/release/ostia-summary.txt"
done
"""


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("envs", nargs="*", default=["cuda-12", "cuda-13"])
    return parser.parse_args(argv)


def find_engine() -> str | None:
    return shutil.which("podman") or shutil.which("docker")


def build_command(envs: list[str], engine: str) -> tuple[list[str], str]:
    """The container's argv and its script; the tarball goes to its stdin."""
    script = SCRIPT.format(envs=" ".join(envs))
    image = config.builtin_defaults()["image"]
    argv = [
        engine,
        "run",
        "--rm",
        "-i",
        "--platform",
        "linux/arm64",
        "-v",
        "ostia-pixi-cache:/root/.cache/rattler",
        image,
        "sh",
        "-c",
        script,
    ]
    return argv, script


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    engine = find_engine()
    if not engine:
        print(
            "error: check-cuda needs podman or docker\n"
            "  fix: install podman (brew install podman && podman machine init && "
            "podman machine start) or Docker Desktop\n"
            "  see: RFC-0001 §1.4",
            file=sys.stderr,
        )
        return 1
    cmd, _ = build_command(args.envs, engine)
    with tempfile.TemporaryDirectory(prefix="ostia-check-cuda-") as d:
        tb = tarball.build(repo_root(), out_dir=Path(d))
        with open(tb.path, "rb") as stdin:
            return subprocess.run(cmd, stdin=stdin).returncode


if __name__ == "__main__":
    sys.exit(main())
