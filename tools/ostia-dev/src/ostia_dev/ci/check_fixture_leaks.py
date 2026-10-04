#!/usr/bin/env python3
"""Scan committed topology fixtures for machine identifiers (RFC-0003 §3, CI pass).

    pixi run ostia-dev check fixture-leaks              # every tracked fixture, exit 1 on findings
    pixi run ostia-dev check fixture-leaks --files F... # only the named files

Output is `path:line: kind`, never the matched value, so a leak is not copied into CI logs.
"""

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ostia_dev.contract import violation
from ostia_dev.paths import ROOT

FIXTURES = "fabric/tests/fixtures/topology"
HEX = r"[0-9a-fA-F]"

# Spans that look like identifiers but are structural; blanked before matching so no
# pattern can see them.
EXEMPT = re.compile(
    rf"{HEX}{{4}}:{HEX}{{2}}:{HEX}{{2}}\.[0-7]"  # PCI bus ID, the join key (RFC-0003 §2)
    rf"|{HEX}{{4}}:\[{HEX}{{2}}-{HEX}{{2}}\]"  # hwloc bridge_pci range
    r"|(?:topo1:)?sha256:[0-9a-f]{64}"
    r"|0x[0-9a-f]{8}(?:,0x[0-9a-f]{8})*"  # hwloc cpuset and nodeset
)

# hwloc info names that may be published (RFC-0003 §2.1). An allowlist fails
# safe: a key the capture tool should have dropped, such as HostName or DMI*, is a leak.
INFO_ALLOWED = {
    "PCIVendor",
    "PCIDevice",
    "CPUVendor",
    "CPUModel",
    "CPUFamilyNumber",
    "CPUModelNumber",
    "GPUVendor",
    "GPUModel",
    "Backend",
    "OstiaPCIeMaxGen",
    "OstiaPCIeMaxWidth",
}
INFO_TAG = re.compile(r"<info\b[^>]*>")
INFO_NAME = re.compile(r"""\bname\s*=\s*(["'])(.*?)\1""")

# Order matters: a line stops at its first kind, and a MAC is also a run of colon groups.
PATTERNS = [
    ("gpu-uuid", re.compile(rf"\bGPU-{HEX}{{8}}-")),
    ("mig-uuid", re.compile(rf"\bMIG-{HEX}{{8}}-")),
    ("instance-id", re.compile(r"\bi-[0-9a-f]{8,17}\b")),
    ("ec2-hostname", re.compile(r"\bip-\d{1,3}-\d{1,3}-\d{1,3}-\d{1,3}\b")),
    # systemd's MAC-derived interface name carries the whole MAC without separators.
    ("enx-name", re.compile(r"\benx[0-9a-f]{12}\b")),
    # An InfiniBand node, port or system-image GUID as sysfs prints it with the colons dropped.
    ("guid", re.compile(rf"(?<!{HEX}){HEX}{{16}}(?!{HEX})")),
    ("mac", re.compile(rf"\b{HEX}{{2}}([:-]){HEX}{{2}}(?:\1{HEX}{{2}}){{4}}\b")),
    ("ipv4", re.compile(r"(?<![\d.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![\d.])")),
    # Only the `::` shorthand or a full eight-group address: shorter colon runs are
    # indistinguishable from `[10de:27b8]` and clock times.
    (
        "ipv6",
        re.compile(
            rf"(?<![0-9A-Za-z:])(?:(?:{HEX}{{1,4}}:){{7}}{HEX}{{1,4}}"
            rf"|(?:{HEX}{{1,4}}(?::{HEX}{{1,4}}){{0,6}})?::(?:{HEX}{{1,4}}(?::{HEX}{{1,4}}){{0,6}})?)"
            r"(?![0-9A-Za-z:])"
        ),
    ),
    # Schemas drop serials entirely (RFC-0003 §2), so the field name alone is a leak.
    ("serial", re.compile(r"(?i)\"serial(?:_?number)?\"\s*:|name=\"SerialNumber\"")),
]


# The capture's own diagnostics and status name what the leak check found on the source
# machine; they stay in the run's artifacts and never become part of a fixture.
CAPTURE_ONLY = {"diagnostics.txt", "status.json"}


@dataclass
class Finding:
    path: str
    line: int
    kind: str


def _blank(m: re.Match) -> str:
    return " " * len(m.group())


def check_text(text: str, path: str) -> list[Finding]:
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        scrubbed = EXEMPT.sub(_blank, line)
        for kind, pat in PATTERNS:
            m = pat.search(scrubbed)
            if not m:
                continue
            if kind == "ipv4" and any(int(g) > 255 for g in m.groups()):
                continue
            out.append(Finding(path, n, kind))
            break
        else:
            names = [m[1] for tag in INFO_TAG.findall(line) for m in INFO_NAME.findall(tag)]
            if any(name not in INFO_ALLOWED for name in names):
                out.append(Finding(path, n, "hwloc-key"))
    return out


def tracked_fixtures(root: Path) -> list[Path]:
    r = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--", FIXTURES],
        capture_output=True,
        check=True,
    )
    return [root / p for p in r.stdout.decode().split("\0") if p]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ostia-dev check fixture-leaks", description=__doc__.splitlines()[0]
    )
    parser.add_argument("--files", nargs="+", type=Path, help="scan these files instead")
    args = parser.parse_args(argv)
    missing = [f for f in args.files or [] if not f.is_file()]
    if missing:
        print(
            violation(
                "fixture-leaks was given files that do not exist",
                [f"missing: {f}" for f in missing],
                "--files names existing fixture files",
                "pass paths to files, or drop --files to scan every tracked fixture",
                "RFC-0003 §3",
            ),
            file=sys.stderr,
        )
        return 2
    files = args.files if args.files else tracked_fixtures(ROOT)
    findings = []
    for f in files:
        if f.name in CAPTURE_ONLY:
            findings.append(Finding(str(f), 0, "capture-only-file"))
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # Fixtures are UTF-8 text; a file that cannot be scanned cannot be shown clean.
            findings.append(Finding(str(f), 0, "unreadable"))
            continue
        findings += check_text(text, str(f))
    for f in findings:
        print(f"{f.path}:{f.line}: {f.kind}")
    if findings:
        print(
            violation(
                "identifiers in topology fixtures",
                [f"{len(findings)} findings; values are not printed"],
                "committed fixtures contain no machine identifiers (RFC-0003 §3)",
                "recapture with ostia-topo-capture, or fix generate.py; "
                "never hand-edit a value out",
                "RFC-0003 §3",
            )
        )
    print(f"fixture-leaks: {len(files)} files, {len(findings)} findings")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
