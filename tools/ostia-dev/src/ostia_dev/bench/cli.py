"""The benchmark tools (RFC-0001 §6, RFC-0005 §2.2)."""

import typer

from ostia_dev import passthrough

bench_app = typer.Typer(help="Benchmark driver, comparison and gate checks.", no_args_is_help=True)

DRIVER = [
    ("run", "Run a benchmark and record schema-1 results."),
    ("convert", "Convert nvbench JSON files to schema-1 results."),
    ("median-seconds", "Print one duration in seconds (for bench overhead)."),
]
for verb, text in DRIVER:
    passthrough.command(bench_app, verb, "bench.ostia_bench", text, (verb,))

TOOLS = [
    ("compare", "bench.compare", "Compare results with a baseline (RFC-0001 §6.3)."),
    ("overhead", "bench.overhead", "The telemetry overhead mechanism (RFC-0001 §6.6)."),
    ("oracles", "bench.oracles", "Check a calibration result against its reference tool."),
    ("bounds", "bench.bounds", "Check gate workloads against their computed bounds."),
    ("capabilities", "bench.capabilities", "Check setups' machines against their gate workloads."),
    ("evidence", "bench.evidence", "Check transport evidence, or probe its counters."),
]
for verb, mod, text in TOOLS:
    passthrough.command(bench_app, verb, mod, text)


def register(app: typer.Typer) -> None:
    app.add_typer(bench_app, name="bench")
