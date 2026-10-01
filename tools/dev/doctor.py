#!/usr/bin/env python3
"""Print the environment and diagnose common problems (RFC-0001, Failure handling).

    pixi run doctor

Prints the same facts as the configure summary (compiler, CUDA on or off and why,
toolkit, architectures, telemetry level, components, dependencies), then one `check:`
line per known problem. Exits 1 when any check fails.
"""

import importlib.metadata
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import urllib.parse
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ostia_dev.paths import build_root, conda_prefix, env_name, repo_root

SUMMARY_KEYS = [
    "compiler",
    "cuda",
    "cuda_toolkit",
    "architectures",
    "telemetry_level",
    "components",
    "dependencies",
    "ccache",
]


def _version(cmd: str) -> str:
    path = shutil.which(cmd)
    if not path:
        return "not found"
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10)
        return f"{out.stdout.splitlines()[0] if out.stdout else '?'} ({path})"
    except (OSError, subprocess.SubprocessError):
        return f"? ({path})"


def _summary(root: Path) -> dict[str, str]:
    path = build_root(root) / "dev" / "ostia-summary.txt"
    if not path.exists():
        return {}
    facts = {}
    for line in path.read_text().splitlines():
        key, _, value = line.partition(": ")
        facts[key] = value
    return facts


class Checks:
    def __init__(self) -> None:
        self.failed = 0

    def ok(self, name: str) -> None:
        print(f"check: {name}: ok")

    def fail(self, name: str, problem: str, fix: str, see: str = "docs/guides/building.md") -> None:
        self.failed += 1
        print(f"check: {name}: FAILED")
        print(f"error: {problem}\n  fix: {fix}\n  see: {see}")


def run_checks(root: Path, prefix: Path | None) -> Checks:
    c = Checks()

    cmake = shutil.which("cmake")
    if prefix and cmake and Path(cmake).resolve() != (prefix / "bin" / "cmake").resolve():
        c.fail(
            "cmake",
            f"the cmake on PATH is not the pixi environment's ({cmake})",
            "run commands through pixi (pixi run build, or pixi shell), "
            "or point your IDE at .pixi/envs/<env>/bin/cmake",
            "RFC-0001 §1.3",
        )
    else:
        c.ok("cmake")

    lock = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain", "pixi.lock"],
        capture_output=True,
        text=True,
    )
    if lock.stdout.strip():
        c.fail(
            "pixi.lock",
            "pixi.lock has uncommitted changes",
            "commit them if you changed pixi.toml, otherwise git checkout pixi.lock",
        )
    else:
        c.ok("pixi.lock")

    if prefix:
        build_libs = list((build_root(root) / "dev").glob("*/libostia-*"))
        stale = []
        for lib in build_libs:
            installed = prefix / "lib" / lib.name
            # Install rewrites the RPATH, so compare times, not bytes: an installed copy
            # older than the build output predates the last native change.
            if installed.exists() and installed.stat().st_mtime + 1 < lib.stat().st_mtime:
                stale.append(lib.name)
        if stale:
            c.fail(
                "installed libraries",
                f"{', '.join(sorted(stale))} in {prefix / 'lib'} are older than the build tree",
                "pixi run py-dev",
                "RFC-0001 §3.5",
            )
        else:
            c.ok("installed libraries")

        layering = json.loads((root / "cmake" / "layering.json").read_text())
        wrong = []
        for comp in layering["components"]:
            if not (root / comp / "python" / "pyproject.toml").exists():
                continue
            direct = _direct_url(comp)
            if direct is None:
                wrong.append(f"ostia-{comp} is not installed as an editable")
            elif Path(direct).resolve() != (root / comp / "python").resolve():
                wrong.append(f"ostia-{comp} is installed from {direct}")
        if wrong:
            c.fail("python editables", "; ".join(wrong), "pixi run py-dev", "RFC-0001 §3.5")
        else:
            c.ok("python editables")

    if sys.platform == "darwin":
        sdk = subprocess.run(["xcrun", "--show-sdk-path"], capture_output=True, text=True)
        major = int(platform.mac_ver()[0].split(".")[0] or 0)
        if sdk.returncode != 0 or not sdk.stdout.strip():
            c.fail("macOS SDK", "xcrun found no macOS SDK", "xcode-select --install")
        elif major < 14:
            c.fail(
                "macOS version",
                f"macOS {platform.mac_ver()[0]} is older than 14.0",
                "use macOS 14 or newer",
                "RFC-0001 §1.2",
            )
        else:
            c.ok("macOS SDK")

    cache = os.environ.get("CPM_SOURCE_CACHE")
    if not cache or not Path(cache).is_dir() or not any(Path(cache).iterdir()):
        c.fail(
            "CPM cache",
            f"the CPM source cache is empty ({cache or 'CPM_SOURCE_CACHE not set'})",
            "pixi run build (fetches the pinned sources once)",
            "RFC-0001 §2.4",
        )
    else:
        c.ok("CPM cache")
    return c


def _installed_flavour(prefix: Path | None) -> str:
    """The telemetry level of the stack installed into the environment (RFC-0001 §5)."""
    if prefix is None:
        return "not running under pixi"
    config = prefix / "lib" / "cmake" / "ostia" / "ostiaConfig.cmake"
    if not config.exists():
        return "none installed (pixi run py-dev)"
    text = config.read_text()
    name = re.search(r'set\(ostia_TELEMETRY "(\w+)"\)', text)
    level = re.search(r"set\(ostia_TELEMETRY_LEVEL (\d)\)", text)
    if not name:
        return "unknown (installed before telemetry levels; pixi run py-dev)"
    return f"{name.group(1)} ({level.group(1) if level else '?'})"


def _direct_url(comp: str) -> str | None:
    try:
        dist = importlib.metadata.distribution(f"ostia-{comp}")
    except importlib.metadata.PackageNotFoundError:
        return None
    text = dist.read_text("direct_url.json")
    if not text:
        return None
    url = json.loads(text).get("url", "")
    return urllib.parse.unquote(url.removeprefix("file://")) or None


def main() -> int:
    root = repo_root()
    prefix = conda_prefix()
    facts = _summary(root)
    if not facts:
        print("not configured: run pixi run build")
    for key in SUMMARY_KEYS:
        print(f"{key}: {facts.get(key, 'not configured')}")
    print(f"pixi_env: {env_name()} ({prefix or 'CONDA_PREFIX not set: not running under pixi'})")
    print(f"cmake: {_version('cmake')}")
    print(f"ninja: {_version('ninja')}")
    print(f"installed_flavour: {_installed_flavour(prefix)}")
    checks = run_checks(root, prefix)
    return 1 if checks.failed else 0


if __name__ == "__main__":
    sys.exit(main())
