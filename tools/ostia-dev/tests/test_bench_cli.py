"""bench run|convert|median-seconds|compare|overhead|oracles|bounds|capabilities|evidence
forward to the moved tools (RFC-0005 §2.2, ruling B1)."""

import json
import shutil
import subprocess
import sys

from ostia_dev.bench import ostia_bench
from ostia_dev.cli import app
from typer.testing import CliRunner


def test_bench_help_is_argparse_with_new_prog():
    result = CliRunner().invoke(app, ["bench", "compare", "--help"])
    assert result.exit_code == 0
    assert result.output.startswith("usage: ostia-dev bench compare")


def test_bench_run_passes_through(monkeypatch):
    seen = {}

    def main(argv):
        seen["argv"] = argv
        return 3

    monkeypatch.setattr(ostia_bench, "main", main)
    result = CliRunner().invoke(app, ["bench", "run", "--bench", "b", "--runs", "3"])
    assert result.exit_code == 3
    assert seen["argv"] == ["run", "--bench", "b", "--runs", "3"]


def test_cli_import_does_not_load_the_moved_tools():
    lazy = ["clang", "ostia_dev.bench.capabilities", "ostia_dev.ci.check_telemetry_macros"]
    code = f"import ostia_dev.cli, sys; assert not [m for m in {lazy!r} if m in sys.modules]"
    assert subprocess.run([sys.executable, "-c", code]).returncode == 0


FAKE = """#!/usr/bin/env python3
import json, sys
open(sys.argv[0] + ".argv", "w").write(json.dumps(sys.argv[1:]))
print(json.dumps({"bench": "x", "params": {}, "unit": "GB/s", "higher_is_better": True,
                  "samples": [1.0]}))
"""


def test_program_args_after_the_separator_reach_the_program(tmp_path):
    """`bench run … -- ARGS` hands ARGS to the program, as remote gate's steps rely on."""
    prog = tmp_path / "prog"
    prog.write_text(FAKE)
    prog.chmod(0o755)
    r = subprocess.run(
        [shutil.which("ostia-dev"), "bench", "run", "--format", "ostia", "--bench", str(prog),
         "--runs", "1", "--run-id", "t", "--out", str(tmp_path / "out"), "--", "--mem", "cuda"],
        capture_output=True,
        text=True,
    )  # fmt: skip
    assert r.returncode == 0, r.stderr
    assert json.loads((tmp_path / "prog.argv").read_text()) == ["--mem", "cuda"]
