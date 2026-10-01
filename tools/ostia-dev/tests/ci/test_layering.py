"""ostia_dev.ci.layering reads cmake/layering.json, the dependency table (RFC-0001 §3.3)."""

from ostia_dev.ci.layering import allowed_text, load_layering


def test_load_layering_reads_the_table():
    assert "telemetry" in load_layering()["components"]


def test_allowed_text():
    table = {"components": {"a": {"depends": []}, "b": {"depends": ["a", "c"]}}}
    assert allowed_text(table, "a") == "nothing"
    assert allowed_text(table, "b") == "a, c"
