#!/usr/bin/env python3
"""Install the Python packages as editables over the native prefix (RFC-0001 §3.5).

Run through pixi, which first builds and installs the native libraries:

    pixi run py-dev          # also the documented rebuild after a native change

Each component with a python/ folder is installed in rank order, as a scikit-build-core
editable that links the one libostia-* in $CONDA_PREFIX/lib. A component whose Python
sources are unchanged is skipped; native changes still arrive through the prefix.
"""

import argparse
import hashlib
import importlib.metadata
import json
import shutil
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.dev._paths import build_root, conda_prefix, env_name, repo_root


def fail(problem: str, fix: str) -> int:
    print(f"error: {problem}\n  fix: {fix}\n  see: RFC-0001 §3.5", file=sys.stderr)
    return 1


def stamp(source: Path, component: str) -> str:
    """Everything the component's extension compiles against: its python/ folder, the
    public headers of the component and of its table dependencies, and the build modules."""
    layering = json.loads((source / "cmake" / "layering.json").read_text())
    deps = layering["components"][component]["depends"]
    trees = [source / component / "python", source / "cmake"]
    trees += [source / c / "include" for c in (component, *deps)]
    h = hashlib.sha256()
    h.update(f"{source.resolve()}|{env_name()}|{sys.version}".encode())
    for tree in trees:
        for f in sorted(tree.rglob("*")):
            if f.is_file() and "__pycache__" not in f.parts:
                h.update(str(f.relative_to(source)).encode())
                h.update(f.read_bytes())
    return h.hexdigest()


def installed(component: str) -> bool:
    try:
        importlib.metadata.distribution(f"ostia-{component}")
    except importlib.metadata.PackageNotFoundError:
        return False
    return True


def run(cmd: list[str], cwd: Path) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=cwd, check=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--preset", default="dev")
    parser.add_argument("--source-root", type=Path, default=repo_root())
    parser.add_argument("--build-root", type=Path)
    parser.add_argument("--force", action="store_true", help="reinstall even when up to date")
    parser.add_argument(
        "--build-native",
        action="store_true",
        help="configure, build and install native code first (tests; pixi does this otherwise)",
    )
    args = parser.parse_args(argv)
    source = args.source_root.resolve()
    build = (args.build_root or build_root(source)).resolve()
    prefix = conda_prefix()
    if prefix is None:
        return fail("CONDA_PREFIX is not set", "run through pixi: pixi run py-dev")

    if args.build_native:
        native = build / args.preset
        run(["cmake", "--preset", args.preset, "-B", str(native)], cwd=source)
        run(["cmake", "--build", str(native)], cwd=source)
        run(["cmake", "--install", str(native), "--prefix", str(prefix)], cwd=source)
        # The record `pixi run clean` reads (ctest's staging install rewrites the other one).
        shutil.copyfile(native / "install_manifest.txt", build / "native-install-manifest.txt")

    if not (prefix / "lib" / "cmake" / "ostia" / "ostiaConfig.cmake").exists():
        return fail(f"native ostia is not installed in {prefix}", "pixi run install-native")

    print(f"installing native libraries into {prefix} (RFC-0001 §3.5); undo with pixi run clean")
    layering = json.loads((source / "cmake" / "layering.json").read_text())
    ranked = sorted(layering["components"], key=lambda c: layering["components"][c]["rank"])
    for component in ranked:
        project = source / component / "python"
        if not (project / "pyproject.toml").exists():
            continue  # placeholders have no Python package
        stamp_file = build / "py" / f"{component}.stamp"
        current = stamp(source, component)
        if (
            not args.force
            and installed(component)
            and stamp_file.exists()
            and stamp_file.read_text() == current
        ):
            print(f"{component}: up to date")
            continue
        run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--no-build-isolation",
                "--no-deps",
                "--quiet",
                "-e",
                ".",
                f"-Cbuild-dir={build / 'py' / component}",
                f"-Ccmake.define.CMAKE_PREFIX_PATH={prefix}",
            ],
            cwd=project,
        )
        stamp_file.parent.mkdir(parents=True, exist_ok=True)
        stamp_file.write_text(current)
        print(f"{component}: installed")
    print("after a native change, rebuild with: pixi run py-dev")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except subprocess.CalledProcessError as e:
        sys.exit(
            fail(
                f"command failed ({e.returncode}): {' '.join(map(str, e.cmd))}",
                "read the output above; pixi run doctor checks the environment",
            )
        )
