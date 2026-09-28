"""PEP 420 namespace packages over one native prefix (RFC-0001 §3.5, Done when)."""

import json
import os
import pathlib
import shutil
import subprocess
import sys

CODE = r"""
import json, os, sys, pathlib, ostia.telemetry, ostia.fabric
import ostia
paths = [str(p) for p in ostia.__path__]
inits = [p for p in paths if (pathlib.Path(p) / "__init__.py").exists()]
if sys.platform == "darwin":
    import ctypes
    lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
    lib._dyld_get_image_name.restype = ctypes.c_char_p
    lib._dyld_get_image_name.argtypes = [ctypes.c_uint32]
    images = [lib._dyld_get_image_name(i).decode() for i in range(lib._dyld_image_count())]
else:
    images = [line.split()[-1] for line in open("/proc/self/maps") if "/" in line]
tele = sorted({os.path.realpath(p) for p in images if "libostia-telemetry" in p})
print(json.dumps({"file": getattr(ostia, "__file__", None), "inits": inits, "tele": tele,
                  "ext": ostia.telemetry._native.__file__,
                  "levels": [ostia.telemetry.build_level(), ostia.fabric.telemetry_build_level()]}))
"""


def _load(run_py) -> dict:
    r = run_py(CODE)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_two_components_one_telemetry(run_py, conda_lib):
    data = _load(run_py)
    print("loaded libostia-telemetry:", data["tele"])  # RFC-0001 §3.5: print and check
    assert data["file"] is None  # a PEP 420 namespace package has no __file__
    assert data["inits"] == []  # no ostia/__init__.py anywhere
    assert len(data["tele"]) == 1  # exactly one libostia-telemetry loaded
    assert pathlib.Path(data["tele"][0]).parent == conda_lib
    assert data["levels"] == [1, 1]


def test_nothing_bundled(site_arch, conda_lib):
    site = pathlib.Path(site_arch)
    assert not list(site.rglob("libostia-*")), "libostia-* bundled into site-packages"
    for record in site.glob("ostia_*.dist-info/RECORD"):
        assert "libostia-" not in record.read_text(), record
    real = {p.resolve() for p in conda_lib.glob("libostia-telemetry*")}
    assert len(real) == 1, real


def test_extension_rpath_is_relative(run_py, conda_lib):
    ext = _load(run_py)["ext"]
    if sys.platform == "darwin":
        out = subprocess.run(["otool", "-l", ext], capture_output=True, text=True).stdout
        lines = out.splitlines()
        rpaths = [
            lines[i + 2].split()[1]
            for i, line in enumerate(lines)
            if line.strip() == "cmd LC_RPATH"
        ]
        want = "@loader_path/"
    else:
        # conda's toolchain activation exports READELF; fall back to binutils on PATH.
        readelf = os.environ.get("READELF") or shutil.which("readelf") or "readelf"
        out = subprocess.run([readelf, "-d", ext], capture_output=True, text=True).stdout
        rpaths = [
            p
            for line in out.splitlines()
            if "(RUNPATH)" in line or "(RPATH)" in line
            for p in line.split("[", 1)[1].rstrip("]").split(":")
        ]
        want = "$ORIGIN/"
    assert len(rpaths) == 1 and rpaths[0].startswith(want), rpaths  # RFC-0001 §3.5
    target = pathlib.Path(ext).parent / rpaths[0].removeprefix(want)
    assert target.resolve() == conda_lib
