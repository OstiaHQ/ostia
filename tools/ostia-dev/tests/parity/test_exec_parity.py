"""Exec rows: the old script and its subcommand, run on copies of the same fixture, give the
same exit code, stdout, stderr and files (RFC-0005 §2.3). Only names and paths may differ,
and `normalise` maps those."""

import json
import random
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from .conftest import REPO, normalise, run_new, run_old, snapshot

pytestmark = pytest.mark.parity

NVBENCH = REPO / "tools/ostia-dev/tests/parity/fixtures/nvbench_noop.json"
LAYERING = REPO / "cmake/layering.json"


def _git_tree(d: Path, files: dict[str, str]) -> None:
    subprocess.run(["git", "init", "-q", str(d)], check=True)
    for rel, text in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)


def layering_bad(d: Path) -> None:
    _git_tree(
        d,
        {
            "cmake/layering.json": LAYERING.read_text(),
            "query/src/plan.cpp": "\n\n#include <ostia/fabric/topology.hpp>\n",
        },
    )


def layering_ok(d: Path) -> None:
    _git_tree(
        d,
        {
            "cmake/layering.json": LAYERING.read_text(),
            "query/src/plan.cpp": "#include <ostia/runtime/api.hpp>\n",
        },
    )


def cpm_unpinned(d: Path) -> None:
    (d / "cmake").mkdir()
    (d / "cmake/Dependencies.cmake").write_text(
        "CPMAddPackage(NAME bar GITHUB_REPOSITORY a/b GIT_TAG v1.0)\n"
    )


def nm_fixture(d: Path) -> None:
    under = "_" if sys.platform == "darwin" else ""
    (d / "nm.sh").write_text(
        "#!/bin/sh\n"
        f"echo '0000000000001120 T {under}ostia_telemetry_build_level'\n"
        f"echo '0000000000001130 T {under}ostia_extra'\n"
    )
    (d / "nm.sh").chmod(0o755)
    (d / "lib.so").write_text("")
    (d / "exports.txt").write_text("ostia_telemetry_build_level\nostia_gone\n")


def public_header_with_config(d: Path) -> None:
    (d / "cmake").mkdir()
    (d / "cmake/layering.json").write_text(LAYERING.read_text())
    h = d / "fabric/include/ostia/fabric/x.h"
    h.parent.mkdir(parents=True)
    h.write_text("#include <ostia/telemetry/config.h>\nint x = OSTIA_COUNT(a);\n")


def narration(d: Path) -> None:
    (d / "a.py").write_text("a = 1\n# Now we increment i\ni += 1\n")


def hook_payload(d: Path) -> str:
    narration(d)
    return json.dumps(
        {
            "tool_name": "Edit",
            "tool_input": {"file_path": str(d / "a.py"), "new_string": "# Now we increment i\n"},
        }
    )


def guide(d: Path) -> None:
    (d / "guide.md").write_text(
        "# x\n<!-- docs-as-test:start -->\n```bash\necho hi > out.txt\n```\n"
        "<!-- docs-as-test:end -->\n"
    )


def clean_fixture(d: Path) -> None:
    installed = d / "prefix/lib/libostia-telemetry.so"
    installed.parent.mkdir(parents=True)
    installed.write_text("")
    build = d / "build/default"
    (build / "dev").mkdir(parents=True)
    (build / "native-install-manifest.txt").write_text(f"{installed}\n")
    (build / "dev/install_manifest.txt").write_text(f"{d}/stage/lib/x.so\n")


def fake_bench(d: Path) -> None:
    shutil.copy(NVBENCH, d / "nvbench.json")
    prog = d / "bench"
    prog.write_text(
        "#!/usr/bin/env python3\nimport shutil, sys\n"
        f"shutil.copy({str(d / 'nvbench.json')!r}, sys.argv[sys.argv.index('--json') + 1])\n"
    )
    prog.chmod(0o755)


def _record(mean: float, run: str, rng: random.Random, bench="example", params=None, n=20):
    return {
        "schema": 1,
        "provenance": {"git_sha": "0000000", "date": "2026-10-01T00:00:00Z", "run_id": run},
        "compat": {
            "gpu": "none",
            "driver": "none",
            "cuda": "none",
            "nic": "none",
            "topology": None,
            "build_level": "off",
            "compiler": "example",
            "deps": "pixi.lock:example",
        },
        "bench": bench,
        "params": params or {"bytes": 1024},
        "unit": "GB/s",
        "higher_is_better": True,
        "samples": [rng.gauss(mean, 0.1) for _ in range(n)],
    }


def records(d: Path) -> None:
    rng = random.Random(1)
    for name, mean in (("base", 40.0), ("same", 40.0), ("slower", 35.0)):
        (d / f"{name}.jsonl").write_text(json.dumps(_record(mean, name, rng)) + "\n")


def seconds(d: Path) -> None:
    (d / "seconds.py").write_text(
        "import os\nprint(1.0 * (1 + float(os.environ.get('OSTIA_BENCH_INJECT_SLOWDOWN') or 0)))\n"
    )


MIB = 1024 * 1024


def calibration(d: Path) -> None:
    rec = _record(
        260.0,
        "c",
        random.Random(2),
        "p2p_copy",
        {"bytes": 1024 * MIB, "direction": "0->1", "concurrency": 1},
        10,
    )
    rec["samples"] = [260.0] * 10
    (d / "results.jsonl").write_text(json.dumps(rec) + "\n")


def dual_link(d: Path) -> None:
    rng = random.Random(3)
    recs = []
    for path, value in (("a", 40.0), ("b", 40.0), ("both", 79.0)):
        r = _record(value, "b", rng, "dual_link", {"path": path, "bytes": MIB}, 10)
        r["samples"] = [value] * 10
        recs.append(r)
    (d / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs))


def nothing(d: Path) -> None:
    pass


@dataclass
class Row:
    id: str
    task: int  # the plan task that makes the row pass
    old: str  # script path in the base tree
    old_args: list[str]
    new_args: list[str]
    fixture: object = nothing
    stdin: object = None  # callable(d) -> str
    repo_root: bool = False
    interpreter: str | None = None
    marks: list = field(default_factory=list)


D = "{d}"
R = str(REPO)
FIG = f"{R}/docs/product/figures/src"
needs_bun = pytest.mark.skipif(not shutil.which("bun"), reason="bun is not on PATH")

ROWS = [
    Row(
        "lint-only-layering",
        3,
        "tools/ci/lint.py",
        ["lint", "--root", D, "--only", "layering"],
        ["lint", "--root", D, "--only", "layering"],
        layering_bad,
    ),
    Row(
        "check-layering-bad",
        3,
        "tools/ci/check_layering.py",
        ["--root", D],
        ["check", "layering", "--root", D],
        layering_bad,
    ),
    Row(
        "check-layering-ok",
        3,
        "tools/ci/check_layering.py",
        ["--root", D],
        ["check", "layering", "--root", D],
        layering_ok,
    ),
    Row(
        "check-cpm-pins",
        3,
        "tools/ci/check_cpm_pins.py",
        ["--root", D],
        ["check", "cpm-pins", "--root", D],
        cpm_unpinned,
    ),
    Row(
        "check-exports",
        3,
        "tools/ci/check_exports.py",
        [
            "--lib",
            f"{D}/lib.so",
            "--expected",
            f"{D}/exports.txt",
            "--level",
            "1",
            "--nm",
            f"{D}/nm.sh",
        ],
        [
            "check",
            "exports",
            "--lib",
            f"{D}/lib.so",
            "--expected",
            f"{D}/exports.txt",
            "--level",
            "1",
            "--nm",
            f"{D}/nm.sh",
        ],
        nm_fixture,
    ),
    Row(
        "check-graph-dot",
        3,
        "tools/ci/check_graph.py",
        ["--dot", f"{R}/tests/cmake/layering/golden.dot"],
        ["check", "graph", "--dot", f"{R}/tests/cmake/layering/golden.dot"],
    ),
    Row(
        "check-macros-public",
        3,
        "tools/ci/check_telemetry_macros.py",
        ["--public", "--root", D],
        ["check", "macros", "--public", "--root", D],
        public_header_with_config,
    ),
    Row(
        "check-comments-files",
        3,
        "tools/ci/check_comments.py",
        [f"{D}/a.py"],
        ["check", "comments", f"{D}/a.py"],
        narration,
    ),
    Row(
        "check-comments-hook",
        3,
        "tools/ci/check_comments.py",
        ["--hook"],
        ["check", "comments", "--hook"],
        stdin=hook_payload,
    ),
    Row(
        "docs-as-test",
        3,
        "tools/ci/docs_as_test.py",
        ["--doc", f"{D}/guide.md", "--cwd", D],
        ["check", "docs-as-test", "--doc", f"{D}/guide.md", "--cwd", D],
        guide,
    ),
    Row("doctor", 4, "tools/dev/doctor.py", [], ["doctor"], repo_root=True),
    Row(
        "clean",
        4,
        "tools/dev/clean.py",
        ["--build-root", f"{D}/build/default", "--skip-pip"],
        ["clean", "--build-root", f"{D}/build/default", "--skip-pip"],
        clean_fixture,
    ),
    Row(
        "docs-index-check", 5, "tools/docs/gen_index.py", ["--check"], ["docs", "index", "--check"]
    ),
    Row(
        "docs-figures",
        5,
        "tools/docs/render-figures.js",
        [FIG, f"{D}/out"],
        ["docs", "figures", FIG, f"{D}/out"],
        interpreter="bun",
        marks=[needs_bun],
    ),
    Row(
        "bench-convert",
        6,
        "tools/bench/ostia_bench.py",
        ["convert", "--run-id", "p", "--out", f"{D}/out", f"{D}/nvbench.json"],
        ["bench", "convert", "--run-id", "p", "--out", f"{D}/out", f"{D}/nvbench.json"],
        fake_bench,
    ),
    Row(
        "bench-run",
        6,
        "tools/bench/ostia_bench.py",
        ["run", "--bench", f"{D}/bench", "--runs", "2", "--run-id", "p", "--out", f"{D}/out"],
        [
            "bench",
            "run",
            "--bench",
            f"{D}/bench",
            "--runs",
            "2",
            "--run-id",
            "p",
            "--out",
            f"{D}/out",
        ],
        fake_bench,
    ),
    Row(
        "bench-median-seconds",
        6,
        "tools/bench/ostia_bench.py",
        ["median-seconds", "--bench", f"{D}/bench"],
        ["bench", "median-seconds", "--bench", f"{D}/bench"],
        fake_bench,
    ),
    Row(
        "bench-compare-pass",
        6,
        "tools/bench/compare.py",
        ["--baseline", f"{D}/base.jsonl", "--candidate", f"{D}/same.jsonl"],
        ["bench", "compare", "--baseline", f"{D}/base.jsonl", "--candidate", f"{D}/same.jsonl"],
        records,
    ),
    Row(
        "bench-compare-regression",
        6,
        "tools/bench/compare.py",
        ["--baseline", f"{D}/base.jsonl", "--candidate", f"{D}/slower.jsonl"],
        ["bench", "compare", "--baseline", f"{D}/base.jsonl", "--candidate", f"{D}/slower.jsonl"],
        records,
    ),
    Row(
        "bench-compare-write-baseline",
        6,
        "tools/bench/compare.py",
        [
            "--write-baseline",
            f"{D}/b.json",
            "--setup",
            "nvlink-node",
            f"{D}/base.jsonl",
            f"{D}/same.jsonl",
        ],
        [
            "bench",
            "compare",
            "--write-baseline",
            f"{D}/b.json",
            "--setup",
            "nvlink-node",
            f"{D}/base.jsonl",
            f"{D}/same.jsonl",
        ],
        records,
    ),
    Row(
        "bench-overhead-self-test",
        6,
        "tools/bench/overhead.py",
        ["--self-test", f"python3 {D}/seconds.py"],
        ["bench", "overhead", "--self-test", f"python3 {D}/seconds.py"],
        seconds,
    ),
    Row(
        "bench-oracles-print",
        6,
        "tools/bench/oracles.py",
        ["--results", f"{D}/results.jsonl", "--oracle", "nvbandwidth", "--print-command"],
        [
            "bench",
            "oracles",
            "--results",
            f"{D}/results.jsonl",
            "--oracle",
            "nvbandwidth",
            "--print-command",
        ],
        calibration,
    ),
    Row(
        "bench-bounds",
        6,
        "tools/bench/bounds.py",
        [f"{D}/results.jsonl"],
        ["bench", "bounds", f"{D}/results.jsonl"],
        dual_link,
    ),
    Row(
        "bench-capabilities",
        6,
        "tools/bench/capabilities.py",
        ["--setups", f"{R}/infra/setups"],
        ["bench", "capabilities", "--setups", f"{R}/infra/setups"],
    ),
    Row(
        "bench-evidence-missing",
        6,
        "tools/bench/evidence.py",
        [f"{D}/missing.json"],
        ["bench", "evidence", f"{D}/missing.json"],
    ),
    Row(
        "bench-evidence-probe-ib",
        6,
        "tools/bench/evidence.py",
        ["--probe", "ib"],
        ["bench", "evidence", "--probe", "ib"],
    ),
]

# Tasks whose subcommands have landed; their rows must pass from then on.
LANDED: set[int] = {3, 4, 5, 6}


def _params():
    for row in ROWS:
        marks = list(row.marks)
        if row.task not in LANDED:
            marks.append(pytest.mark.xfail(strict=True, reason=f"lands in Task {row.task}"))
        yield pytest.param(row, id=row.id, marks=marks)


def _fill(args: list[str], d: Path) -> list[str]:
    return [a.replace("{d}", str(d)) for a in args]


@pytest.mark.parametrize("row", list(_params()))
def test_exec_parity(row: Row, base_tree: Path, tmp_path: Path):
    old_dir, new_dir = tmp_path / "old", tmp_path / "new"
    for d in (old_dir, new_dir):
        d.mkdir()
        row.fixture(d)
    old_in = row.stdin(old_dir) if row.stdin else None
    new_in = row.stdin(new_dir) if row.stdin else None
    old = run_old(
        base_tree,
        row.old,
        _fill(row.old_args, old_dir),
        cwd=old_dir,
        stdin=old_in,
        repo_root=row.repo_root,
        interpreter=row.interpreter,
    )
    new = run_new(_fill(row.new_args, new_dir), cwd=new_dir, stdin=new_in)
    swaps = [(str(old_dir), str(new_dir)), (str(base_tree), str(REPO))]
    assert (new.returncode, new.stdout, new.stderr) == (
        old.returncode,
        normalise(old.stdout, swaps),
        normalise(old.stderr, swaps),
    )
    assert snapshot(new_dir) == snapshot(old_dir, swaps)
