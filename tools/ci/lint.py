#!/usr/bin/env python3
"""Run the fast checks, or apply the formatters (ADR-0013, RFC-0001 §4.1 lint row).

    pixi run lint    # clang-format, ruff, gersemi, include layering, CPM pins, comments,
                     # docs index
    pixi run fmt     # apply clang-format, ruff and gersemi

Each failing check prints an error-message-contract block with its fix; the run ends
with one summary line. File lists come from git, so only tracked files are checked.
"""

import argparse
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.ci._contract import ROOT
from tools.ci.check_comments import language

CPP = (".h", ".hpp", ".c", ".cpp", ".cu", ".cuh")
VENDORED = {"cmake/CPM.cmake"}
SHOWN_LINES = 20


def file_lists(root: Path) -> dict[str, list[str]]:
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True
    ).stdout.decode()
    files = sorted(
        f
        for f in out.split("\0")
        if f and (root / f).is_file() and "/tests/golden/" not in f  # golden = expected output
    )
    return {
        "cpp": [f for f in files if f.endswith(CPP)],
        "cmake": [
            f
            for f in files
            if (f.endswith(".cmake") or Path(f).name == "CMakeLists.txt") and f not in VENDORED
        ],
        "python": [f for f in files if f.endswith(".py")],
        "comments": [f for f in files if language(f) and f not in VENDORED],
    }


def _checks(root: Path, lists: dict[str, list[str]], mode: str) -> list[tuple]:
    """(name, command, files, fix) per check; files None means the tool finds its own."""
    py = sys.executable
    if mode == "fmt":
        return [
            ("clang-format", ["clang-format", "-i"], lists["cpp"], ""),
            ("ruff-format", ["ruff", "format"], lists["python"], ""),
            ("ruff-fix", ["ruff", "check", "--fix"], lists["python"], ""),
            ("gersemi", ["gersemi", "-i"], lists["cmake"], ""),
        ]
    return [
        ("clang-format", ["clang-format", "--dry-run", "-Werror"], lists["cpp"], "pixi run fmt"),
        (
            "ruff-check",
            ["ruff", "check"],
            lists["python"],
            "pixi run fmt, then fix the rest by hand",
        ),
        ("ruff-format", ["ruff", "format", "--check"], lists["python"], "pixi run fmt"),
        ("gersemi", ["gersemi", "--check"], lists["cmake"], "pixi run fmt"),
        ("layering", [py, str(ROOT / "tools/ci/check_layering.py"), "--root", str(root)], None, ""),
        ("cpm-pins", [py, str(ROOT / "tools/ci/check_cpm_pins.py"), "--root", str(root)], None, ""),
        (
            "telemetry-headers",
            [py, str(ROOT / "tools/ci/check_telemetry_macros.py"), "--public", "--root", str(root)],
            None,
            "",
        ),
        (
            "comments",
            [py, str(ROOT / "tools/ci/check_comments.py")],
            lists["comments"],
            "",
        ),
        (
            "docs-index",
            [py, str(ROOT / "tools/docs/gen_index.py"), "--check"],
            None,
            "pixi run docs-index",
        ),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", choices=["lint", "fmt"])
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--only", action="append", help="run only the named check(s)")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    lists = file_lists(root)
    checks = [c for c in _checks(root, lists, args.mode) if not args.only or c[0] in args.only]
    failed = 0
    for name, cmd, files, fix in checks:
        if files is not None and not files:
            continue  # no files of this kind
        r = subprocess.run([*cmd, *(files or [])], cwd=root, capture_output=True, text=True)
        if r.returncode == 0:
            continue
        failed += 1
        output = (r.stdout + r.stderr).strip()
        if name in ("layering", "cpm-pins", "telemetry-headers", "comments"):
            print(output)  # already in contract form
            continue
        lines = output.splitlines()
        print(f"error: {name} found problems")
        for line in lines[:SHOWN_LINES]:
            print(f"  {line}")
        if len(lines) > SHOWN_LINES:
            print(f"  ... {len(lines) - SHOWN_LINES} more lines")
        if fix:
            print(f"  fix: {fix}")
        print("  see: ADR-0013" if name != "docs-index" else "  see: docs/README.md")
    print(f"{args.mode}: {len(checks)} checks, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
