"""Bench tests never find a real ostia-topo-capture: the variable is cleared and PATH is bare."""

import pytest


@pytest.fixture(autouse=True)
def no_capture_tool(monkeypatch):
    monkeypatch.delenv("OSTIA_TOPO_CAPTURE", raising=False)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
