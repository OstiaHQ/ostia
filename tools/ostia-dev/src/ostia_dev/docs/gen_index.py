#!/usr/bin/env python3
"""Regenerate the RFC and ADR index in docs/README.md from document front matter.

Usage:
    pixi run ostia-dev docs index          # rewrite the index in place
    pixi run ostia-dev docs index --check  # exit 1 if the index is out of date
"""

import argparse
import re
import sys

from ostia_dev.paths import ROOT

README = ROOT / "docs" / "README.md"
KINDS = (("RFC", ROOT / "docs" / "rfcs"), ("ADR", ROOT / "docs" / "adr"))
START, END = "<!-- index:start -->", "<!-- index:end -->"
REQUIRED = ("number", "title", "status", "components", "created")
STATUSES = {"Draft", "In review", "Accepted", "Implemented", "Rejected", "Superseded"}


def front_matter(path):
    """Parse the flat `key: value` front matter used by RFCs and ADRs."""
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not match:
        raise SystemExit(f"{path}: missing front matter")
    meta = {}
    for line in match.group(1).splitlines():
        if not line.strip() or ":" not in line:
            continue
        key, value = line.split(":", 1)
        value = value.split(" #", 1)[0].strip()
        if value.startswith("[") and value.endswith("]"):
            value = [v.strip() for v in value[1:-1].split(",") if v.strip()]
        meta[key.strip()] = value
    return meta


def collect():
    rows = []
    for kind, folder in KINDS:
        for path in sorted(folder.glob("[0-9][0-9][0-9][0-9]-*.md")):
            meta = front_matter(path)
            if meta.get("status") == "Template":
                continue
            missing = [k for k in REQUIRED if not meta.get(k)]
            if missing:
                raise SystemExit(f"{path}: front matter is missing {', '.join(missing)}")
            if meta["status"] not in STATUSES:
                raise SystemExit(f"{path}: unknown status {meta['status']!r}")
            if int(meta["number"]) != int(path.name[:4]):
                raise SystemExit(f"{path}: front matter number does not match file name")
            rows.append((kind, int(meta["number"]), meta, path.relative_to(README.parent)))
    return rows


def render(rows):
    if not rows:
        return "No RFCs or ADRs yet."
    lines = [
        "| Document | Title | Status | Components | Created |",
        "| --- | --- | --- | --- | --- |",
    ]
    for kind, number, meta, rel in rows:
        components = (
            ", ".join(meta["components"])
            if isinstance(meta["components"], list)
            else meta["components"]
        )
        link = f"[{kind}-{number:04d}]({rel.as_posix()})"
        cells = [link, meta["title"], meta["status"], components, str(meta["created"])]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ostia-dev docs index", description=__doc__.splitlines()[0]
    )
    parser.add_argument("--check", action="store_true", help="exit 1 if the index is out of date")
    args = parser.parse_args(argv)
    text = README.read_text(encoding="utf-8")
    if START not in text or END not in text:
        raise SystemExit(f"{README}: index markers not found")
    head, rest = text.split(START, 1)
    _, tail = rest.split(END, 1)
    updated = f"{head}{START}\n{render(collect())}\n{END}{tail}"
    if args.check:
        if updated != text:
            print("docs/README.md index is out of date; run pixi run ostia-dev docs index")
            return 1
        return 0
    README.write_text(updated, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
