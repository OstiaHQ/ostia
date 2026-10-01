#!/usr/bin/env python3
"""Run a guide's marked command blocks verbatim (RFC-0001 §4.1 docs-as-test).

    pixi run ostia-dev check docs-as-test --doc docs/guides/building.md

Runs every fenced block between `<!-- docs-as-test:start -->` and
`<!-- docs-as-test:end -->` with `bash -e`, in order, in --cwd (default: the repository
root), and records the time taken in $GITHUB_STEP_SUMMARY when it is set.
"""

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ostia_dev.paths import ROOT

START, END = "<!-- docs-as-test:start -->", "<!-- docs-as-test:end -->"
FENCE = re.compile(r"^```[a-z]*\n(.*?)^```", re.MULTILINE | re.DOTALL)


class MarkerError(ValueError):
    pass


def blocks(text: str) -> list[str]:
    found: list[str] = []
    pos = 0
    while (start := text.find(START, pos)) != -1:
        end = text.find(END, start)
        if end == -1:
            line = text.count("\n", 0, start) + 1
            raise MarkerError(f"line {line}: docs-as-test:start has no matching docs-as-test:end")
        found += [m.group(1) for m in FENCE.finditer(text[start:end])]
        pos = end + len(END)
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ostia-dev check docs-as-test", description=__doc__.splitlines()[0]
    )
    parser.add_argument("--doc", type=Path, default=ROOT / "docs" / "guides" / "building.md")
    parser.add_argument("--cwd", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    try:
        found = blocks(args.doc.read_text())
    except MarkerError as e:
        print(f"error: {args.doc}: {e}\n  fix: close the block with {END}\n  see: RFC-0001 §4.1")
        return 1
    started = time.monotonic()
    for i, block in enumerate(found, start=1):
        print(f"== {args.doc.name} block {i} of {len(found)}:\n{block}", flush=True)
        r = subprocess.run(["bash", "-e", "-c", block], cwd=args.cwd)
        if r.returncode != 0:
            print(
                f"error: block {i} of {args.doc} failed with exit code {r.returncode}\n"
                "  fix: make the documented commands work, or update the guide\n"
                "  see: RFC-0001 §4.1 (docs-as-test)"
            )
            return 1
    seconds = time.monotonic() - started
    print(f"docs-as-test: {len(found)} blocks from {args.doc.name} passed in {seconds:.0f} s")
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a") as f:
            f.write(
                f"**docs-as-test:** {len(found)} blocks from `{args.doc.name}` in {seconds:.0f} s\n"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
