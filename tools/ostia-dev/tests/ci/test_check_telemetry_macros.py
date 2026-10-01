"""Tests for ostia_dev/ci/check_telemetry_macros.py (RFC-0001 §5)."""

from pathlib import Path

import pytest
from ostia_dev.ci.check_telemetry_macros import scan_args, scan_public_headers

LAYERING = Path(__file__).resolve().parents[4] / "cmake" / "layering.json"


def repo(root: Path, files: dict[str, str]) -> Path:
    (root / "cmake").mkdir(parents=True, exist_ok=True)
    (root / "cmake" / "layering.json").write_text(LAYERING.read_text())
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root


@pytest.mark.parametrize(
    "text,reason",
    [
        ("inline void f() { OSTIA_COUNT(x, 1); }\n", "uses OSTIA_COUNT"),
        ("#include <ostia/telemetry/config.h>\n", "includes ostia/telemetry/config.h"),
        ('#include "ostia/telemetry/instrument.hpp"\n', "includes ostia/telemetry/instrument.hpp"),
        ("// no telemetry here\n", None),
    ],
)
def test_public_headers(tmp_path, text, reason):
    repo(
        tmp_path,
        {"fabric/include/ostia/fabric/x.hpp": text, "fabric/src/ok.cpp": "OSTIA_COUNT(x, 1);\n"},
    )
    found = scan_public_headers(tmp_path)
    assert [v.reason for v in found] == ([] if reason is None else [reason])


SOURCE = """\
#define OSTIA_COUNT(h, n) do { if constexpr (false) { (void)(h); (void)(n); } } while (0)
int pop_next();
[[gnu::pure]] int pure_fn(int);
constexpr int twice(int x) { return 2 * x; }
void f(int x, int a, int b) {
    OSTIA_COUNT(1, pop_next());
    OSTIA_COUNT(1, pure_fn(x));
    OSTIA_COUNT(1, twice(x));
    OSTIA_COUNT(1, x++);
    OSTIA_COUNT(1, x = 2);
    OSTIA_COUNT(1, a == b);
    OSTIA_COUNT(1, static_cast<long>(a));
    OSTIA_COUNT(1, pop_next()); // ostia-telemetry: args-pure
    OSTIA_COUNT(1, a += b);
}
"""


def test_macro_arguments(tmp_path):
    src = tmp_path / "f.cpp"
    src.write_text(SOURCE)
    found = scan_args(src, ["-std=c++20"])
    assert [(v.line, v.reason) for v in found] == [
        (6, "calls pop_next(), which may have side effects"),
        (9, "uses ++, which changes state"),
        (10, "assigns with =, which changes state"),
        (14, "assigns with +=, which changes state"),
    ]


def test_unparseable_file_is_an_error_not_a_pass(tmp_path):
    src = tmp_path / "g.cpp"
    src.write_text("#include <ostia/telemetry/no_such_header.hpp>\nvoid g() {}\n")
    [v] = scan_args(src, ["-std=c++20"])
    assert v.reason.startswith("could not be parsed")
