"""The C++ half of `ostia-dev coverage`: merge the raw profiles the coverage preset's tests
wrote, then export an lcov file for Codecov and a text summary (llvm-profdata, llvm-cov)."""

import argparse
import os
import subprocess
import sys
from pathlib import Path

from ostia_dev.contract import violation

# Test, fuzz and benchmark code, dependencies and generated files are not what we measure.
IGNORE = r"(/\.cache/|/build/|/tests?/|/fuzz/|/bench/|/_deps/)"


def binaries(tree: Path) -> list[Path]:
    """The instrumented Ostia binaries: component libraries, test executables and tools."""
    found = []
    for p in sorted(tree.rglob("*")):
        if "_deps" in p.parts or "CMakeFiles" in p.parts or p.is_symlink() or not p.is_file():
            continue
        library = p.name.startswith("libostia-") and (".so" in p.name or p.suffix == ".dylib")
        program = p.name.startswith("ostia") and p.suffix == "" and os.access(p, os.X_OK)
        if library or program:
            found.append(p)
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", required=True, type=Path, help="The coverage build tree.")
    args = parser.parse_args(argv)
    tree: Path = args.build

    raw = sorted((tree / "profiles").glob("*.profraw"))
    objects = binaries(tree)
    if not raw or not objects:
        print(
            violation(
                "no coverage data to report",
                [f"raw profiles: {len(raw)} in {tree / 'profiles'}", f"binaries: {len(objects)}"],
                "the coverage preset's tests write one raw profile per process",
                "pixi run -e clang ostia-dev coverage",
                "docs/guides/building.md",
            ),
            file=sys.stderr,
        )
        return 1

    merged = tree / "coverage.profdata"
    code = subprocess.call(["llvm-profdata", "merge", "-sparse", *map(str, raw), "-o", str(merged)])
    if code:
        return code
    common = [
        f"-instr-profile={merged}",
        f"-ignore-filename-regex={IGNORE}",
        str(objects[0]),
        *(a for o in objects[1:] for a in ("-object", str(o))),
    ]
    with open(tree / "cpp.lcov", "w") as out:
        code = subprocess.call(["llvm-cov", "export", "-format=lcov", *common], stdout=out)
    if code:
        return code
    report = subprocess.run(
        ["llvm-cov", "report", *common], capture_output=True, text=True, check=False
    )
    sys.stdout.write(report.stdout)
    sys.stderr.write(report.stderr)
    (tree / "cpp-summary.txt").write_text(report.stdout)
    return report.returncode


if __name__ == "__main__":
    sys.exit(main())
