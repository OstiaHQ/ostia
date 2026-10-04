#!/usr/bin/env python3
"""One `ostia-topo diff` case: a fixture against a mutated copy of itself (RFC-0003 §5).

    diff-case.py --tool <ostia-topo> --fixture <dir> --mutation none|nvlink|renumber
                 --expect <exit code> [--output <regex>]

`nvlink` turns one more active NVLink in nvml.json inactive, which changes the identity;
`renumber` moves every bus ID on bus 0x21 to bus 0x2a, which must not. Standard library only.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def _inactivate_one_nvlink(copy: Path) -> None:
    path = copy / "nvml.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    for gpu in doc["gpus"]:
        for link in gpu["nvlinks"] if isinstance(gpu["nvlinks"], list) else []:
            if link["state"] == "active":
                link["state"] = "inactive"
                path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                return
    raise SystemExit("diff-case: the fixture has no active NVLink to turn off")


def _renumber(copy: Path) -> None:
    # The new bus stays inside the fixture's bridge range [20-2f], so the bridge still covers it.
    for path in copy.iterdir():
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace("0000:21:", "0000:2a:"), encoding="utf-8")


MUTATIONS = {"none": lambda copy: None, "nvlink": _inactivate_one_nvlink, "renumber": _renumber}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tool", required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--mutation", choices=sorted(MUTATIONS), required=True)
    parser.add_argument("--expect", type=int, required=True)
    parser.add_argument("--output", help="a regex the standard output must contain")
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="ostia-topo-diff-") as tmp:
        copy = Path(tmp) / args.fixture.name
        shutil.copytree(args.fixture, copy, ignore=shutil.ignore_patterns("expected.json"))
        MUTATIONS[args.mutation](copy)
        proc = subprocess.run(
            [args.tool, "diff", str(args.fixture), str(copy)],
            check=False,
            capture_output=True,
            text=True,
        )
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    if proc.returncode != args.expect:
        print(f"diff-case: exit {proc.returncode}, expected {args.expect}", file=sys.stderr)
        return 1
    if args.output and not re.search(args.output, proc.stdout):
        print(f"diff-case: the output does not match {args.output}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
