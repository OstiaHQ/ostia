"""A component built at one telemetry level refuses a libostia-telemetry built at another
(RFC-0001 §5): ImportError naming both levels, and the process keeps running."""

import os
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest

from .conftest import ROOT

pytestmark = pytest.mark.slow

CODE = """
try:
    import ostia.fabric
except ImportError as e:
    print("caught:", e)
else:
    print("imported")
"""


def _lib(directory: Path, name: str) -> Path:
    [lib] = [p for p in directory.glob(f"lib{name}.*") if p.suffix in (".so", ".dylib")]
    return lib


def test_mismatched_flavour_raises_import_error(tmp_path):
    prefix = Path(os.environ["CONDA_PREFIX"])
    # An off-flavour libostia-telemetry, built from this checkout.
    build = tmp_path / "off"
    cfg = [
        "cmake",
        "-S",
        str(ROOT),
        "-B",
        str(build),
        "-G",
        "Ninja",
        "-DOSTIA_TELEMETRY=off",
        "-DOSTIA_BUILD_TESTS=OFF",
        "-DOSTIA_BUILD_FABRIC=OFF",
        "-DOSTIA_BUILD_EXCHANGE=OFF",
        "-DOSTIA_BUILD_RUNTIME=OFF",
        "-DOSTIA_BUILD_QUERY=OFF",
    ]
    subprocess.run(cfg, check=True, capture_output=True)
    subprocess.run(["cmake", "--build", str(build)], check=True, capture_output=True)

    # A prefix holding the environment's fabric (and its extension) with that telemetry.
    lib = tmp_path / "prefix" / "lib"
    site = lib / Path(sysconfig.get_paths()["platlib"]).relative_to(prefix / "lib")
    lib.mkdir(parents=True)
    shutil.copy2(_lib(prefix / "lib", "ostia-fabric"), lib)
    shutil.copy2(_lib(build / "telemetry", "ostia-telemetry"), lib)
    for comp in ("telemetry", "fabric"):
        dest = site / "ostia" / comp
        dest.mkdir(parents=True)
        shutil.copy2(ROOT / comp / "python" / "src" / "ostia" / comp / "__init__.py", dest)
        installed = Path(sysconfig.get_paths()["platlib"]) / "ostia" / comp
        for ext in installed.glob("_native*"):
            shutil.copy2(ext, dest)

    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONPATH"] = str(site)
    r = subprocess.run([sys.executable, "-S", "-c", CODE], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr  # the process survives
    assert "caught:" in r.stdout, r.stdout + r.stderr
    assert "off (0)" in r.stdout and "debug (3)" in r.stdout, r.stdout
