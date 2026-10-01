"""bench run|convert|median-seconds|compare|overhead|oracles|bounds|capabilities|evidence
forward to the moved tools (RFC-0005 §2.2, ruling B1)."""

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
