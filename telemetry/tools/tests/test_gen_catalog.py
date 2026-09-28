"""Tests for telemetry/tools/gen_catalog.py (RFC-0002 §1, RFC-0001 §5)."""

import importlib.util
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("gen_catalog", HERE.parent / "gen_catalog.py")
gen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen)

CATALOG = """
[[metric]]
name = "ostia.fabric.bytes_sent"
kind = "counter"
unit = "By"
description = "Bytes handed to a transport for sending."
dimensions = ["transport"]

[[metric]]
name = "ostia.fabric.progress_loop_latency"
kind = "histogram"
unit = "ns"

[[attribute]]
name = "transport"
values = ["cuda_ipc", "ucx_rc", "ucx_tcp"]

[[event]]
name = "fabric.chunk_send"
args = { arg0 = "bytes", arg1 = "chunk" }
"""


def test_golden_header(tmp_path):
    toml = tmp_path / "telemetry.toml"
    toml.write_text(CATALOG)
    header = gen.render("fabric", gen.load(toml, "fabric"), "fabric/telemetry.toml")
    assert header == (HERE / "golden" / "fabric_catalog.hpp").read_text()


@pytest.mark.parametrize(
    "text,message",
    [
        (
            '[[metric]]\nname = "ostia.query.x"\nkind = "counter"\nunit = "1"\n',
            "must start with ostia.fabric.",
        ),
        (
            '[[metric]]\nname = "ostia.fabric.Bad"\nkind = "counter"\nunit = "1"\n',
            "lower_snake_case",
        ),
        (
            '[[metric]]\nname = "ostia.fabric.x"\nkind = "meter"\nunit = "1"\n',
            "kind must be one of",
        ),
        ('[[metric]]\nname = "ostia.fabric.x"\nkind = "counter"\n', "needs a unit"),
        (
            '[[metric]]\nname = "ostia.fabric.x"\nkind = "counter"\nunit = "1"\ndimensions = ["peer"]\n',
            "undeclared attribute 'peer'",
        ),
        (
            '[[metric]]\nname = "ostia.fabric.x"\nkind = "counter"\nunit = "1"\n'
            '[[metric]]\nname = "ostia.fabric.x"\nkind = "gauge"\nunit = "1"\n',
            "declared twice",
        ),
        ('[[event]]\nname = "query.y"\n', "must start with fabric."),
    ],
)
def test_rejects_bad_catalogs(tmp_path, text, message):
    toml = tmp_path / "telemetry.toml"
    toml.write_text(text)
    with pytest.raises(gen.CatalogError, match=message):
        gen.load(toml, "fabric")


def test_main_reports_errors_in_contract_form(tmp_path, capsys):
    toml = tmp_path / "telemetry.toml"
    toml.write_text('[[metric]]\nname = "ostia.fabric.x"\nkind = "meter"\nunit = "1"\n')
    rc = gen.main(
        ["--component", "fabric", "--catalog", str(toml), "--out", str(tmp_path / "o.hpp")]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "error:" in err and "fix:" in err and "RFC-0002 §1" in err


def test_empty_catalog_gives_an_empty_namespace(tmp_path):
    toml = tmp_path / "telemetry.toml"
    toml.write_text("")
    header = gen.render("exchange", gen.load(toml, "exchange"), "exchange/telemetry.toml")
    assert "namespace ostia::exchange::catalog {" in header
