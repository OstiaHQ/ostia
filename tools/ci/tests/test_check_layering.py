"""Tests for tools/ci/check_layering.py (RFC-0001 §3.3, enforcement part 3)."""

from pathlib import Path

import pytest

from tools.ci.check_layering import main, scan

LAYERING = Path(__file__).resolve().parents[3] / "cmake" / "layering.json"


def make_repo(root: Path, files: dict[str, str]) -> None:
    (root / "cmake").mkdir(parents=True)
    (root / "cmake" / "layering.json").write_text(LAYERING.read_text())
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


CASES = [
    ("query/src/plan.cpp", "#include <ostia/fabric/topology.hpp>\n", "upward"),
    ("query/src/plan.cpp", '#  include "ostia/fabric/topology.hpp"\n', "upward"),
    ("fabric/src/a.cu", "#include <ostia/exchange/x.hpp>\n", "upward"),
    ("fabric/include/ostia/fabric/a.cuh", "#include <ostia/runtime/x.hpp>\n", "upward"),
    ("fabric/src/a.cpp", '#include "telemetry/src/detail.hpp"\n', "src"),
    ("fabric/src/a.cpp", '#include "../../telemetry/src/detail.hpp"\n', "relative"),
    ("fabric/src/a.cpp", '#include "../../telemetry/include/ostia/telemetry/t.h"\n', "relative"),
    ("fabric/src/sub/a.cpp", '#include "x/../../../../telemetry/x.h"\n', "relative"),
    ("query/src/plan.cpp", "#include <ostia/runtime/api.hpp>\n", None),
    ("fabric/src/a.cpp", '#include "local.hpp"\n', None),
    ("fabric/src/a/b.cpp", '#include "../local.hpp"\n', None),
    ("fabric/src/a.cpp", '#include "fabric/src/own.hpp"\n', None),
    ("fabric/src/a.cpp", "#include <ostia/fabric/fabric.h>\n", None),
    ("telemetry/src/a.cpp", "#include <opentelemetry/src/x.h>\n", None),
    ("fabric/src/a.cpp", "#include <vector>\n", None),
]


@pytest.mark.parametrize("path,content,reason", CASES)
def test_rules(tmp_path, path, content, reason):
    make_repo(tmp_path, {path: content})
    found = scan(tmp_path)
    if reason is None:
        assert found == []
    else:
        assert [v.reason for v in found] == [reason]  # one violation per line
        assert found[0].line == 1


def test_transitive_exposure_passes(tmp_path):
    make_repo(
        tmp_path,
        {
            "runtime/include/ostia/runtime/api.hpp": "#include <ostia/fabric/topology.hpp>\n",
            "query/src/plan.cpp": "#include <ostia/runtime/api.hpp>\n",
        },
    )
    assert scan(tmp_path) == []


def test_build_directories_are_skipped(tmp_path):
    make_repo(tmp_path, {"fabric/build/gen.cpp": "#include <ostia/query/x.hpp>\n"})
    assert scan(tmp_path) == []


def test_message_follows_contract(tmp_path, capsys):
    make_repo(tmp_path, {"query/src/plan.cpp": "\n\n#include <ostia/fabric/topology.hpp>\n"})
    assert main(["--root", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "error: query/src/plan.cpp:3 includes <ostia/fabric/topology.hpp>" in out
    assert "query may depend on: runtime, exchange, telemetry" in out
    assert "RFC-0001 §3.3" in out


def test_clean_repo_exits_zero(tmp_path, capsys):
    make_repo(tmp_path, {"fabric/src/a.cpp": "#include <ostia/telemetry/telemetry.h>\n"})
    assert main(["--root", str(tmp_path)]) == 0
