#!/usr/bin/env python3
"""Check resolved link edges against the layering table (RFC-0001 §3.3, part 2).

The configure-time link walk cannot evaluate generator expressions, so CI also checks
the graph CMake resolves:

    cmake --preset dev --graphviz=build/default/dev/graph/ostia.dot
    python tools/ci/check_graph.py --dot build/default/dev/graph/ostia.dot
"""

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.ci._contract import allowed_text, load_layering, violation

NODE = re.compile(r'^\s*"(?P<id>[^"]+)"\s*\[\s*label\s*=\s*"(?P<label>[^"]*)"')
EDGE = re.compile(r'^\s*"(?P<src>[^"]+)"\s*->\s*"(?P<dst>[^"]+)"')
COMPONENT = re.compile(r"^ostia(?:::|_)([a-z]+)$")


@dataclass
class Violation:
    source: str
    target: str


def _component(label: str, components: set[str]) -> str | None:
    for part in label.replace("\\n", "\n").splitlines():
        m = COMPONENT.match(part.strip().strip("()"))
        if m and m.group(1) in components:
            return m.group(1)
    return None


def violations(dot_text: str, layering: dict | None = None) -> list[Violation]:
    layering = layering or load_layering()
    components = set(layering["components"])
    nodes: dict[str, str] = {}
    found: list[Violation] = []
    for line in dot_text.splitlines():
        if m := NODE.match(line):
            c = _component(m.group("label"), components)
            if c:
                nodes[m.group("id")] = c
        elif m := EDGE.match(line):
            src, dst = nodes.get(m.group("src")), nodes.get(m.group("dst"))
            if src is None or dst is None or src == dst:
                continue
            if dst not in layering["components"][src]["depends"]:
                found.append(Violation(src, dst))
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dot", type=Path, required=True, help="output of cmake --graphviz")
    parser.add_argument("--layering", type=Path, help="default: cmake/layering.json")
    args = parser.parse_args(argv)
    layering = load_layering(args.layering)
    found = violations(args.dot.read_text(), layering)
    for v in found:
        print(
            violation(
                f"ostia_{v.source} links ostia_{v.target} (resolved link graph, {args.dot})",
                [f"{v.source} may depend on: {allowed_text(layering, v.source)}"],
                "a component links only the entries in its row of the dependency table",
                "remove the link (check generator expressions in target_link_libraries), "
                "or change the dependency table through an RFC",
                "RFC-0001 §3.3",
            )
        )
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
