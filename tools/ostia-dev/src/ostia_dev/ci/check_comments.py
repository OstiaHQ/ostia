#!/usr/bin/env python3
"""Flag comments that break the comment policy (ADR-0015).

    pixi run ostia-dev check comments FILE...   # check whole files, exit 1 on findings
    pixi run ostia-dev check comments --hook    # Claude Code PostToolUse hook (stdin JSON)

The rules are heuristics, tuned to stay quiet on Ostia's own code: commented-out code,
banners, step narration, change-log notes, comments that restate the next line, TODOs
without an issue, and (hook only) over-commented edits. Hook mode checks only the text
the edit added and exits 2 so the agent sees the findings; any internal error exits 0,
because a broken checker must never stop an agent's work. `comment-ok` on a line
silences it.
"""

import argparse
import ast
import io
import json
import re
import sys
import textwrap
import tokenize
from dataclasses import dataclass
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ostia_dev.contract import violation
from ostia_dev.paths import ROOT

HASH = {".py", ".cmake", ".sh"}
SLASH = {".h", ".hpp", ".c", ".cpp", ".cu", ".cuh", ".inl", ".ipp"}

EXEMPT = re.compile(
    r"comment-ok|noqa|NOLINT|SPDX-|type:\s*ignore|pragma|clang-format\s+(on|off)"
    r"|fmt:\s*(on|off|skip)|gersemi:|pylint:|ruff:|pyright:|mypy:|IWYU|-\*-"
)
BANNER = re.compile(r"[-=*#/~_]{4,}")
NARRATION = re.compile(
    r"^(step\s*\d+\b|now,?\s+(we|let'?s|i)\b|here,?\s+we\b|(first|next|then|finally),?\s+we\b"
    r"|let'?s\s|this\s+(line|code|block)\b"
    r"|we\s+(now\s+)?(call|set|create|loop|check|return|define|initiali[sz]e|import)\b)",
    re.I,
)
CHANGELOG = re.compile(
    r"^(added|changed|updated|fixed|removed|modified|refactored|replaced|moved)\s+"
    r"(the|a|an|new|this|these)\b|^(new|changed|updated|fixed)\s*:"
    r"|as requested|per (the |your )?(user|request)|\((new|changed|updated)\)",
    re.I,
)
TODO = re.compile(r"\b(TODO|FIXME|XXX)\b")
ISSUE = re.compile(r"#\d+|https?://|[A-Z]+-\d+")
CPP_CODE = re.compile(r"[;{}]\s*$|^(#\s*include\b|return\b|if\s*\(|for\s*\(|while\s*\()")
CMAKE_CODE = re.compile(r"^[a-z_]+\s*\([^<\[]+\)\s*$")  # <x> or [x] marks a signature doc
PROSE = re.compile(r"\b[a-z]+ [a-z]+ [a-z]+ [a-z]+\b")
PY_STATEMENTS = (
    ast.Assign,
    ast.AugAssign,
    ast.AnnAssign,
    ast.Import,
    ast.ImportFrom,
    ast.Return,
    ast.Delete,
    ast.Raise,
    ast.Assert,
    ast.If,
    ast.For,
    ast.While,
    ast.With,
    ast.FunctionDef,
    ast.ClassDef,
)
# Words that carry no meaning of their own in a "restates" comment: articles, glue and
# the verbs agents use to narrate an obvious line ("Increment the counter").
STOPWORDS = set(
    (  # noqa: SIM905 -- one string reads better than a literal with a word per line
        "a an the to of and or in on for with from by is are be it its this that we our into at"
        " as then now here new value values variable variables call calls get gets set sets"
        " create creates make makes initialize initialise init increment increments decrement"
        " add adds return returns loop loops over through each every check checks store stores"
        " save saves define defines import imports update updates assign assigns run runs"
    ).split()
)

RULES = {
    "commented-code": (
        "code is not kept in comments; git has the history",
        "delete it, or restore it as live code",
    ),
    "banner": (
        "no divider or banner comments",
        "delete it; if the file needs sections, split it or use functions",
    ),
    "narration": (
        "comments say why, not the steps the code takes",
        "delete it, or say why the step is needed",
    ),
    "changelog": (
        "comments describe the code, not the edit; history belongs in the commit message",
        "delete it, and put the note in the commit or pull request description",
    ),
    "restates": (
        "a comment must add something the code does not already say",
        "delete it, or explain why the line is there",
    ),
    "todo": ("a TODO names its issue", "link the issue, as in TODO(#123), or delete it"),
    "density": (
        "match the comment density of the surrounding code",
        "keep only comments that explain why, an invariant or a hazard",
    ),
}


@dataclass
class Comment:
    line: int
    body: str
    full_line: bool  # nothing but the comment on its line
    block: bool  # /* */ comment; not checked for commented-out code


@dataclass
class Finding:
    path: str
    line: int
    rule: str
    text: str


def language(path: str) -> str | None:
    p = Path(path)
    if p.suffix in SLASH:
        return "cpp"
    if p.name == "CMakeLists.txt":
        return "cmake"
    return p.suffix[1:] if p.suffix in HASH else None


def _hash_naive(text: str) -> list[Comment]:
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        quote = None
        for i, ch in enumerate(line):
            if quote:
                quote = None if ch == quote else quote
            elif ch in "'\"":
                quote = ch
            elif ch == "#":
                out.append(Comment(n, line[i + 1 :], not line[:i].strip(), False))
                break
    return out


def _hash_comments(text: str, lang: str) -> list[Comment]:
    if lang != "py":
        return _hash_naive(text)
    # Edit snippets often start indented; dedent keeps line numbers and lets tokenize run.
    text = textwrap.dedent(text)
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return _hash_naive(text)
    lines = text.splitlines()
    return [
        Comment(t.start[0], t.string[1:], not lines[t.start[0] - 1][: t.start[1]].strip(), False)
        for t in tokens
        if t.type == tokenize.COMMENT
    ]


def _slash_comments(text: str) -> list[Comment]:
    out, in_block = [], False
    for n, line in enumerate(text.splitlines(), 1):
        i, quote, code = 0, None, ""
        while i < len(line):
            if in_block:
                end = line.find("*/", i)
                body = line[i : end if end >= 0 else len(line)].strip().lstrip("*")
                out.append(Comment(n, body, not code.strip(), True))
                if end < 0:
                    break
                in_block, i = False, end + 2
                continue
            ch, two = line[i], line[i : i + 2]
            if quote:
                if ch == "\\":
                    i += 1
                elif ch == quote:
                    quote = None
            elif ch in "'\"":
                quote = ch
            elif two == "//":
                rest = line[i + 2 :]
                rest = rest[1:] if rest[:1] in ("/", "!") else rest  # Doxygen /// and //!
                out.append(Comment(n, rest, not code.strip(), False))
                break
            elif two == "/*":
                in_block, i = True, i + 2
                continue
            code += ch
            i += 1
    return out


def comments(text: str, lang: str) -> list[Comment]:
    return _slash_comments(text) if lang == "cpp" else _hash_comments(text, lang)


def _is_py_code(body: str) -> bool:
    for src in (body, body + "\n    pass"):
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        stmt = tree.body[0] if len(tree.body) == 1 else None
        return isinstance(stmt, PY_STATEMENTS) or (
            isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)
        )
    return False


def _words(s: str) -> list[str]:
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", s)
    return [w.lower().rstrip("s") or w.lower() for w in re.findall(r"[A-Za-z]+", s)]


def _restates(body: str, next_code: str) -> bool:
    words = _words(body)
    content = [w for w in words if w not in STOPWORDS]
    if len(words) < 2 or len(words) > 8 or not content:
        return False
    ident = set(_words(next_code.replace("_", " ")))
    return sum(w in ident for w in content) / len(content) >= 0.75


def check_text(text: str, lang: str, path: str, hook: bool = False, offset: int = 0) -> list:
    lines = text.splitlines()
    found = comments(text, lang)
    comment_lines = {c.line for c in found if c.full_line}
    out: list[Finding] = []

    def add(c: Comment, rule: str) -> None:
        out.append(Finding(path, c.line + offset, rule, lines[c.line - 1].strip()))

    for c in found:
        raw = lines[c.line - 1]
        if EXEMPT.search(raw) or (c.line == 1 and raw.startswith("#!")):
            continue
        body = c.body.strip()
        if not body:
            continue
        # A bare divider is always flagged; a named section banner only in new edits, so
        # the section headers already in long files stay until someone touches them.
        banner_words = len(BANNER.sub(" ", body).split()) if BANNER.search(body) else None
        if banner_words == 0 or (hook and banner_words is not None and banner_words <= 4):
            add(c, "banner")
        elif TODO.search(body) and not ISSUE.search(body):
            add(c, "todo")
        elif CHANGELOG.search(body):
            add(c, "changelog")
        elif NARRATION.search(body):
            add(c, "narration")
        elif c.full_line and not c.block and _is_code(body, lang):
            add(c, "commented-code")
        elif c.full_line and _next_code_restated(c, body, lines, comment_lines):
            add(c, "restates")
    code_lines = [s for s in lines if s.strip()]
    if hook and len(comment_lines) >= 6 and len(comment_lines) > 0.4 * len(code_lines):
        first = min(comment_lines)
        out.append(Finding(path, first + offset, "density", f"{len(comment_lines)} comment lines"))
    return out


def _is_code(body: str, lang: str) -> bool:
    if lang == "py":
        return _is_py_code(body)
    if lang == "cpp":
        return bool(CPP_CODE.search(body)) and not PROSE.search(body)
    return lang == "cmake" and bool(CMAKE_CODE.match(body))


def _next_code_restated(c: Comment, body: str, lines: list[str], comment_lines: set) -> bool:
    # Only a lone comment line: the first line of a longer explanation is not a label.
    if c.line - 1 in comment_lines or c.line + 1 in comment_lines or c.line >= len(lines):
        return False
    nxt = lines[c.line]
    return bool(nxt.strip()) and _restates(body, nxt)


def report(findings: list[Finding]) -> str:
    blocks = []
    for rule, (text, fix) in RULES.items():
        hits = [f for f in findings if f.rule == rule]
        if hits:
            details = [f"{f.path}:{f.line}: {f.text}" for f in hits]
            blocks.append(
                violation(f"{rule}: comment breaks ADR-0015", details, text, fix, "ADR-0015")
            )
    return "\n".join(blocks)


def _display(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def check_files(paths: list[Path]) -> list[Finding]:
    out = []
    for p in paths:
        lang = language(str(p))
        if lang:
            out += check_text(p.read_text(errors="replace"), lang, _display(p))
    return out


def check_hook(payload: dict) -> list[Finding]:
    tool_input = payload.get("tool_input") or {}
    path = tool_input.get("file_path") or ""
    lang = language(path)
    if not lang:
        return []
    if "content" in tool_input:
        snippets = [tool_input["content"]]
    elif "edits" in tool_input:
        snippets = [e.get("new_string", "") for e in tool_input["edits"]]
    else:
        snippets = [tool_input.get("new_string", "")]
    try:
        current = Path(path).read_text(errors="replace")
    except OSError:
        current = ""
    out = []
    for s in snippets:
        at = current.find(s) if s else -1
        offset = current.count("\n", 0, at) if at >= 0 else 0
        out += check_text(s, lang, _display(Path(path)), hook=True, offset=offset)
    return out


def main(argv: list[str] | None = None, stdin=None) -> int:
    parser = argparse.ArgumentParser(
        prog="ostia-dev check comments", description=__doc__.splitlines()[0]
    )
    parser.add_argument("files", nargs="*", type=Path)
    parser.add_argument("--hook", action="store_true", help="read a PostToolUse payload on stdin")
    args = parser.parse_args(argv)
    if not args.hook:
        findings = check_files(args.files)
        if findings:
            print(report(findings))
        print(f"comments: {len(args.files)} files, {len(findings)} findings")
        return 1 if findings else 0
    try:
        findings = check_hook(json.load(stdin or sys.stdin))
    except Exception:
        return 0
    if not findings:
        return 0
    print(report(findings), file=sys.stderr)
    print("The edit was applied; revise or remove these comments (ADR-0015).", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
