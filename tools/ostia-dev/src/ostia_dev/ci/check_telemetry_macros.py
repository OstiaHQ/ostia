#!/usr/bin/env python3
"""Check how the telemetry macros are used (RFC-0001 §5).

    pixi run ostia-dev check macros --public      # fast: part of pixi run ostia-dev lint
    pixi run ostia-dev check macros --build build/default/dev

1. Public headers (every `<component>/include/`) must not use OSTIA_COUNT,
   OSTIA_TRACE_EVENT or OSTIA_DEBUG_CHECK, or include ostia/telemetry/config.h or
   instrument.hpp, so installed headers are the same at every level.
2. Macro arguments are not evaluated below the macro's level, so they must not change
   state. Every call site in the compile database is parsed with libclang; calls,
   assignments and ++/-- in arguments fail. Calls to functions declared constexpr or
   [[gnu::pure]] / [[gnu::const]] are allowed. Any other exception needs a
   `// ostia-telemetry: args-pure` comment on the line, which review must accept.
"""

import argparse
import json
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ostia_dev.ci.layering import load_layering
from ostia_dev.contract import violation
from ostia_dev.paths import ROOT

MACROS = ("OSTIA_COUNT", "OSTIA_TRACE_EVENT", "OSTIA_DEBUG_CHECK")
MACRO_USE = re.compile(r"\b(" + "|".join(MACROS) + r")\s*\(")
LEVEL_INCLUDE = re.compile(
    r'^\s*#\s*include\s*[<"](ostia/telemetry/(?:config\.h|instrument\.hpp))[>"]'
)
HEADER_EXT = {".h", ".hpp", ".cuh", ".inl", ".ipp"}
SOURCE_EXT = {".c", ".cpp", ".cu"}
ESCAPE = "ostia-telemetry: args-pure"
ASSIGN = {"=", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "<<=", ">>="}
NOT_CALLS = {
    "static_cast",
    "const_cast",
    "reinterpret_cast",
    "dynamic_cast",
    "sizeof",
    "alignof",
    "decltype",
    "noexcept",
    "if",
    "while",
    "for",
    "switch",
    "return",
}


@dataclass
class Violation:
    path: str
    line: int
    reason: str


def scan_public_headers(root: Path) -> list[Violation]:
    found: list[Violation] = []
    for comp in load_layering(root / "cmake" / "layering.json")["components"]:
        include = root / comp / "include"
        if not include.is_dir():
            continue
        for f in sorted(include.rglob("*")):
            if f.suffix not in HEADER_EXT:
                continue
            for number, line in enumerate(f.read_text(errors="replace").splitlines(), 1):
                rel = f.relative_to(root).as_posix()
                if m := MACRO_USE.search(line):
                    found.append(Violation(rel, number, f"uses {m.group(1)}"))
                elif m := LEVEL_INCLUDE.match(line):
                    found.append(Violation(rel, number, f"includes {m.group(1)}"))
    return found


def _pure_functions(tu, cindex) -> dict[str, bool]:
    """Function name -> True only if every declaration with that name is constexpr/pure."""
    kinds = {
        cindex.CursorKind.FUNCTION_DECL,
        cindex.CursorKind.CXX_METHOD,
        cindex.CursorKind.FUNCTION_TEMPLATE,
    }
    pure_attrs = {cindex.CursorKind.PURE_ATTR, cindex.CursorKind.CONST_ATTR}
    result: dict[str, bool] = {}
    for c in tu.cursor.walk_preorder():
        if c.kind not in kinds:
            continue
        head = []
        for t in c.get_tokens():
            if t.spelling == c.spelling:
                break
            head.append(t.spelling)
        pure = (
            "constexpr" in head
            or "consteval" in head
            or any(ch.kind in pure_attrs for ch in c.get_children())
        )
        result[c.spelling] = result.get(c.spelling, True) and pure
    return result


def _arguments(tokens: list[str]) -> list[list[str]]:
    """Split `NAME ( a , b )` into argument token lists, respecting nesting."""
    args, current, depth = [], [], 0
    for tok in tokens[2:]:  # skip the macro name and "("
        if tok in "([{":
            depth += 1
        elif tok in ")]}":
            if depth == 0:
                break
            depth -= 1
        if tok == "," and depth == 0:
            args.append(current)
            current = []
        else:
            current.append(tok)
    args.append(current)
    return args


def _resource_dir() -> list[str]:
    """libclang's builtin headers (stdarg.h, ...) from the environment it came with."""
    dirs = sorted(Path(sys.prefix, "lib", "clang").glob("*/include/stdarg.h"))
    return ["-resource-dir", str(dirs[-1].parent.parent)] if dirs else []


def scan_args(path: Path, compile_args: list[str]) -> list[Violation]:
    from clang import cindex

    index = cindex.Index.create()
    tu = index.parse(
        str(path),
        args=[*compile_args, *_resource_dir()],
        options=cindex.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD,
    )
    lines = path.read_text(errors="replace").splitlines()
    found: list[Violation] = []
    rel = path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else str(path)
    fatal = [d for d in tu.diagnostics if d.severity >= cindex.Diagnostic.Error]
    if fatal:
        # Unexpanded macros would pass silently; an unparseable file is a failure.
        d = fatal[0]
        return [Violation(rel, d.location.line, f"could not be parsed: {d.spelling}")]
    pure = _pure_functions(tu, cindex)
    for c in tu.cursor.get_children():
        if c.kind != cindex.CursorKind.MACRO_INSTANTIATION or c.spelling not in MACROS:
            continue
        if c.location.file is None or Path(c.location.file.name).resolve() != path.resolve():
            continue
        line = c.location.line
        if ESCAPE in lines[line - 1]:
            continue
        tokens = [t.spelling for t in c.get_tokens()]
        for arg in _arguments(tokens):
            reason = None
            for i, tok in enumerate(arg):
                if tok in ("++", "--"):
                    reason = f"uses {tok}, which changes state"
                elif tok in ASSIGN:
                    reason = f"assigns with {tok}, which changes state"
                elif (
                    i + 1 < len(arg)
                    and arg[i + 1] == "("
                    and re.match(r"^[A-Za-z_]\w*$", tok)
                    and tok not in NOT_CALLS
                    and not pure.get(tok, False)
                ):
                    reason = f"calls {tok}(), which may have side effects"
                if reason:
                    break
            if reason:
                found.append(Violation(rel, line, reason))
                break
    return found


_IMPLICIT: dict[str, list[str]] = {}


def _implicit_includes(compiler: str) -> list[str]:
    """The compiler's built-in include directories (GCC's libstdc++ paths are not in the
    compile database, and libclang would not find them)."""
    if compiler not in _IMPLICIT:
        dirs: list[str] = []
        try:
            r = subprocess.run(
                [compiler, "-xc++", "-E", "-v", "-"], input="", capture_output=True, text=True
            )
            inside = False
            for line in r.stderr.splitlines():
                if line.startswith("#include <...> search starts here"):
                    inside = True
                elif line.startswith("End of search list"):
                    break
                elif inside and not line.strip().endswith("(framework directory)"):
                    dirs += ["-isystem", line.strip()]
        except OSError:
            pass
        _IMPLICIT[compiler] = dirs
    return _IMPLICIT[compiler]


def _compile_args(entry: dict) -> list[str]:
    args = entry.get("arguments") or shlex.split(entry["command"])
    keep: list[str] = []
    skip = False
    for a in args[1:]:
        if skip:
            skip = False
            continue
        if a in ("-o", "-c", "-MF", "-MT", "-MQ", "--dependency-file") or a == entry["file"]:
            skip = a != entry["file"]
            continue
        if a.startswith(("-o", "-MD", "-MF", "-fmodules", "-fdeps", "-fmodule-mapper")):
            continue
        keep.append(a)
    if "clang" not in Path(args[0]).name:
        keep += _implicit_includes(args[0])
    return keep


def scan_build(root: Path, build: Path) -> list[Violation]:
    db = json.loads((build / "compile_commands.json").read_text())
    components = set(load_layering(root / "cmake" / "layering.json")["components"])
    found: list[Violation] = []
    seen: set[Path] = set()
    for entry in db:
        path = Path(entry["file"]).resolve()
        if path in seen or path.suffix not in SOURCE_EXT or not path.is_relative_to(root):
            continue
        parts = path.relative_to(root).parts
        if parts[0] not in components:
            continue
        seen.add(path)
        found += scan_args(path, _compile_args(entry))
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ostia-dev check macros", description=__doc__.splitlines()[0]
    )
    parser.add_argument("--root", type=Path, default=ROOT)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--public", action="store_true", help="only the public-header rule")
    mode.add_argument("--build", type=Path, help="build dir with compile_commands.json")
    args = parser.parse_args(argv)
    found = scan_public_headers(args.root)
    for v in found:
        print(
            violation(
                f"{v.path}:{v.line} {v.reason}",
                ["public headers must not depend on the telemetry level"],
                "instrumentation macros and config.h stay out of */include/",
                "move the instrumentation into a source file",
                "RFC-0001 §5",
            )
        )
    if args.build:
        args_found = scan_build(args.root, args.build)
        for v in args_found:
            print(
                violation(
                    f"{v.path}:{v.line} a telemetry macro argument {v.reason}",
                    ["macro arguments are not evaluated below the macro's level"],
                    "arguments must not call, assign, increment or decrement",
                    f"compute the value first, or mark the line '// {ESCAPE}' for review",
                    "RFC-0001 §5",
                )
            )
        found += args_found
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
