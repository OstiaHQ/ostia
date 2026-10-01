"""The hard switch (RFC-0005 §2.3): no old pixi task survives, and CI, the hooks and the
presets name only ostia-dev."""

import json
import re
import tomllib

from ostia_dev.paths import ROOT

ALLOWED = {"ostia-dev", "_configure", "kind", "pytest", "ctest", "cmake", "run-clang-tidy"}


def test_pixi_keeps_only_internal_tasks():
    tasks = tomllib.loads((ROOT / "pixi.toml").read_text())["tasks"]
    assert set(tasks) == {"_configure"}


def test_no_gpu_ci_preset():
    presets = json.loads((ROOT / "CMakePresets.json").read_text())
    for kind in ("configurePresets", "buildPresets", "testPresets"):
        assert "gpu-ci" not in {p["name"] for p in presets.get(kind, [])}, kind


def test_workflows_and_hooks_name_only_ostia_dev():
    files = [*(ROOT / ".github" / "workflows").glob("*.yml"), ROOT / ".pre-commit-config.yaml"]
    found = []
    for f in files:
        for word in re.findall(
            r"pixi run((?: -e (?:\$\{\{[^}]*\}\}|\S+)| --frozen)*) ([\w\-.$]+)", f.read_text()
        ):
            if word[1] not in ALLOWED:
                found.append(f"{f.name}: pixi run{word[0]} {word[1]}")
    assert found == []
