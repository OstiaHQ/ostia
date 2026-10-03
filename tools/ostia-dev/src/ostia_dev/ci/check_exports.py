#!/usr/bin/env python3
"""Check the dynamic symbols a library exports (RFC-0001 §5).

    ostia-dev check exports --lib build/default/dev/telemetry/libostia-telemetry.dylib \\
        --expected telemetry/abi/exports.txt --level 3 --nm nm

Every telemetry flavour exports exactly the C ABI listed in telemetry/abi/exports.txt,
so a flavour is swapped by swapping the library. An `off` build also must not contain
any OpenTelemetry, counter, ring or exporter symbol. CI runs this for all four levels.
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

# Symbols the linker defines in every ELF shared object.
LINKER = {"_init", "_fini", "__bss_start", "_edata", "_end", "__end__", "_etext"}
# The bounds the linker defines for each section named like a C identifier, e.g. the
# __llvm_prf_* sections of a coverage build.
SECTION_BOUND = re.compile(r"__(start|stop)_[A-Za-z_][A-Za-z0-9_]*")
DEFINED = set("TDBRVWSGI")
RUNTIME = re.compile(
    r"opentelemetry|(?<![a-z])otel(?![a-z])|(?<![a-z])counter(?![a-z])|trace_ring|ring_buffer|exporter",
    re.IGNORECASE,
)


def exported(nm_output: str, platform: str) -> list[str]:
    names = []
    for line in nm_output.splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[-2] not in DEFINED:
            continue
        name = parts[-1]
        if platform == "darwin" and name.startswith("_"):
            name = name[1:]  # Mach-O prefixes C symbols with an underscore
        if name not in LINKER and not SECTION_BOUND.fullmatch(name):
            names.append(name)
    return sorted(set(names))


def check(symbols: list[str], expected: list[str], level: int) -> list[str]:
    problems = [f"unexpected: {s}" for s in sorted(set(symbols) - set(expected))]
    problems += [f"missing: {s}" for s in sorted(set(expected) - set(symbols))]
    if level == 0:
        problems += [f"runtime symbol in an off build: {s}" for s in symbols if RUNTIME.search(s)]
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ostia-dev check exports", description=__doc__.splitlines()[0]
    )
    parser.add_argument("--lib", type=Path, required=True)
    parser.add_argument("--expected", type=Path, required=True)
    parser.add_argument("--level", type=int, required=True)
    parser.add_argument("--nm", default="nm")
    args = parser.parse_args(argv)
    flags = ["-gU"] if sys.platform == "darwin" else ["-D", "--defined-only"]
    out = subprocess.run(
        [args.nm, *flags, str(args.lib)], capture_output=True, text=True, check=True
    )
    symbols = exported(out.stdout, sys.platform)
    expected = [
        line.strip()
        for line in args.expected.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    problems = check(symbols, expected, args.level)
    for p in problems:
        print(
            f"error: {args.lib.name}: {p}\n"
            f"  rule: every flavour exports exactly {args.expected}\n"
            "  fix: hide the symbol (OSTIA_*_EXPORT only on the C ABI), or update the list "
            "together with the RFC that adds the function\n"
            "  see: RFC-0001 §5, RFC-0002 §9"
        )
    if not problems:
        print(f"{args.lib.name}: exports match {args.expected.name} ({len(symbols)} symbols)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
