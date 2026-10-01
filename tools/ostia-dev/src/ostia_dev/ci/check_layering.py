#!/usr/bin/env python3
"""Check #include lines against the layering table (RFC-0001 §3.3, part 3).

Scans each component's own files. An <ostia/X/...> include must name the component
itself or one in its row of cmake/layering.json; includes that reach into another
component's src/, and relative includes that leave the component's folder, fail too.
Exposure through another component's public headers is allowed: only a component's own
files are checked. Every line is scanned, including comments and `#if 0` blocks, on
purpose: a disabled upward include is still a dependency waiting to happen.
"""

import argparse
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ostia_dev.ci.layering import allowed_text, load_layering
from ostia_dev.contract import violation
from ostia_dev.paths import ROOT

EXTENSIONS = {".h", ".hpp", ".c", ".cpp", ".cu", ".cuh", ".inl", ".ipp"}
INCLUDE = re.compile(r'^\s*#\s*include\s*([<"])([^>"]+)[>"]')
SRC = re.compile(r"^(?:\./)?([a-z]+)/src/")
PUBLIC = re.compile(r"^ostia/([a-z]+)/")
SKIP_DIRS = {"build", ".pixi", ".cache"}


@dataclass
class Violation:
    path: str
    line: int
    text: str
    component: str
    reason: str  # relative | src | upward


def _files(root: Path, component: str):
    base = root / component
    if not base.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in sorted(filenames):
            p = Path(dirpath) / name
            if p.suffix in EXTENSIONS:
                yield p


def _check(inc: str, angle: bool, file: Path, comp: str, root: Path, layering: dict) -> str | None:
    components = layering["components"]
    if ".." in Path(inc).parts:
        # Angle includes and <ostia/...> paths resolve against each -I directory, so a
        # ".." there can reach anything; only a quoted, file-relative path is checkable.
        if angle or PUBLIC.match(inc):
            return "relative"
        target = Path(os.path.normpath(file.parent / inc))
        if not target.is_relative_to(root / comp):
            return "relative"
    if (m := SRC.match(inc)) and m.group(1) in components and m.group(1) != comp:
        return "src"
    if (m := PUBLIC.match(inc)) and m.group(1) in components:
        other = m.group(1)
        if other != comp and other not in components[comp]["depends"]:
            return "upward"
    return None


def scan(root: Path) -> list[Violation]:
    root = root.resolve()
    layering = load_layering(root / "cmake" / "layering.json")
    found: list[Violation] = []
    for comp in layering["components"]:
        for file in _files(root, comp):
            text = file.read_text(errors="replace")
            for number, line in enumerate(text.splitlines(), start=1):
                m = INCLUDE.match(line)
                if not m:
                    continue
                open_, inc = m.groups()
                reason = _check(inc, open_ == "<", file, comp, root, layering)
                if reason:
                    spelled = f"<{inc}>" if open_ == "<" else f'"{inc}"'
                    rel = file.relative_to(root).as_posix()
                    found.append(Violation(rel, number, spelled, comp, reason))
    return found


FIXES = {
    "upward": "use an allowed component's API, or change the dependency table through an RFC",
    "src": "include the other component's public header as <ostia/<component>/...>",
    "relative": "include the other component's public header as <ostia/<component>/...>",
}
RULES = {
    "upward": "a component includes only its own headers and those of its table row",
    "src": "a component's src/ is private to it",
    "relative": "relative includes stay inside the component's folder; no '..' after ostia/",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ostia-dev check layering", description=__doc__.splitlines()[0]
    )
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root")
    args = parser.parse_args(argv)
    found = scan(args.root)
    layering = load_layering(args.root / "cmake" / "layering.json")
    for v in found:
        print(
            violation(
                f"{v.path}:{v.line} includes {v.text}",
                [f"{v.component} may depend on: {allowed_text(layering, v.component)}"],
                RULES[v.reason],
                FIXES[v.reason],
                "RFC-0001 §3.3",
            )
        )
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
