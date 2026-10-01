#!/usr/bin/env python3
"""Check that every CPMAddPackage pins a tag and a full commit SHA (RFC-0001 §2.4).

The pinned form, which Renovate's regex manager also updates (ostia_cpm_add is the
wrapper in cmake/Dependencies.cmake that announces the pin before fetching):

    ostia_cpm_add(
      NAME googletest
      GITHUB_REPOSITORY google/googletest
      VERSION 1.18.0
      GIT_TAG 063de7e9578f82b369302001269680b4b1553359
    )
"""

import argparse
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ostia_dev.contract import violation
from ostia_dev.paths import ROOT

SKIP_DIRS = {"build", ".pixi", ".cache", ".git", ".superpowers"}
SKIP_FILES = {"cmake/CPM.cmake"}
CALL = re.compile(r"\b(?:CPMAddPackage|ostia_cpm_add)\s*\(")
GIT_TAG = re.compile(r"^\s*GIT_TAG\s+(\S+)(?:[ \t]*#[ \t]*(\S+))?", re.MULTILINE)
VERSION = re.compile(r"^\s*VERSION\s+(\S+)", re.MULTILINE)
NAME = re.compile(r"^\s*NAME\s+(\S+)", re.MULTILINE)
SHA = re.compile(r"^[0-9a-f]{40}$")


@dataclass
class Unpinned:
    path: str
    line: int
    name: str
    reason: str


def _cmake_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if name == "CMakeLists.txt" or name.endswith(".cmake"):
                p = Path(dirpath) / name
                if p.relative_to(root).as_posix() not in SKIP_FILES:
                    yield p


def _body(text: str, start: int) -> str:
    depth, i = 0, start
    while i < len(text):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[start + 1 : i]
        i += 1
    return text[start + 1 :]


def _reason(body: str) -> str | None:
    tag = GIT_TAG.search(body)
    if not tag:
        return "no GIT_TAG with a commit SHA (short or URL form)"
    sha, comment = tag.groups()
    if not SHA.match(sha):
        return f"GIT_TAG {sha} is not a full 40-character commit SHA"
    version = VERSION.search(body)
    if not version:
        return "no VERSION naming the release tag"
    if comment and comment.lstrip("v") != version.group(1).lstrip("v"):
        return f"VERSION {version.group(1)} does not match the tag comment {comment}"
    return None


def scan(root: Path) -> list[Unpinned]:
    root = root.resolve()
    found: list[Unpinned] = []
    for path in sorted(_cmake_files(root)):
        text = path.read_text(errors="replace")
        for m in CALL.finditer(text):
            line_start = text.rfind("\n", 0, m.start()) + 1
            if "#" in text[line_start : m.start()]:
                continue  # a call in a comment
            body = _body(text, m.end() - 1)
            if body.strip() == "${ARGN}":
                continue  # the wrapper forwarding its arguments
            reason = _reason(body)
            if reason:
                name = NAME.search(body)
                found.append(
                    Unpinned(
                        path.relative_to(root).as_posix(),
                        text.count("\n", 0, m.start()) + 1,
                        name.group(1) if name else body.strip().strip('"'),
                        reason,
                    )
                )
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ostia-dev check cpm-pins", description=__doc__.splitlines()[0]
    )
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    found = scan(args.root)
    for u in found:
        print(
            violation(
                f"{u.path}:{u.line} CPMAddPackage(NAME {u.name}) is not pinned",
                [u.reason],
                "every CPMAddPackage pins VERSION (the release tag) "
                "and GIT_TAG (its full commit SHA)",
                "resolve the SHA with `gh api repos/<owner>/<repo>/git/ref/tags/<tag>` "
                "and write the pinned form shown in "
                "tools/ostia-dev/src/ostia_dev/ci/check_cpm_pins.py",
                "RFC-0001 §2.4",
            )
        )
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
