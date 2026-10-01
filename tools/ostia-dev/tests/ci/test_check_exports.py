"""Tests for ostia_dev/ci/check_exports.py (RFC-0001 §5: identical exports in every flavour)."""

from ostia_dev.ci.check_exports import check, exported

MACOS = """
0000000000003f80 T _ostia_telemetry_build_level
"""
LINUX = """
0000000000001100 T _fini
0000000000001000 T _init
0000000000001120 T ostia_telemetry_build_level
0000000000004010 B __bss_start
0000000000004010 D _edata
0000000000004018 B _end
"""


def test_normalizes_macos_and_linux():
    assert exported(MACOS, "darwin") == ["ostia_telemetry_build_level"]
    assert exported(LINUX, "linux") == ["ostia_telemetry_build_level"]


def test_matching_exports_pass():
    assert check(["ostia_telemetry_build_level"], ["ostia_telemetry_build_level"], level=1) == []


def test_extra_or_missing_symbols_fail():
    problems = check(
        ["ostia_telemetry_build_level", "ostia_extra"],
        ["ostia_telemetry_build_level", "ostia_gone"],
        1,
    )
    assert any("unexpected: ostia_extra" in p for p in problems)
    assert any("missing: ostia_gone" in p for p in problems)


def test_off_rejects_runtime_symbols():
    syms = ["ostia_telemetry_build_level", "_ZN13opentelemetry3sdkE", "ostia_trace_ring_push"]
    problems = check(syms, syms, level=0)
    assert any("opentelemetry" in p for p in problems)
    assert any("trace_ring" in p for p in problems)
    assert check(["ostia_telemetry_to_string"], ["ostia_telemetry_to_string"], level=0) == []
