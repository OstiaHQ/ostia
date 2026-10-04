#!/usr/bin/env python3
"""End-to-end run of ostia-topo-capture on the committed fake machine (RFC-0003 §1-§4).

    e2e.py --binary <ostia-topo-capture> --nvml-dir <dir of the fake libnvidia-ml.so.1> --data <dir>

hwloc loads the identifier-laden hwloc-input.xml through HWLOC_XMLFILE, sysfs is the committed
capture-root, and NVML is the test-built fake configured by OSTIA_FAKE_NVML. The verbs probe is
always off (--no-verbs) so the runner's own RDMA devices never reach a capture. Standard library
only: the ctest and the cross-language pytest both import run_case from here.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# The fake NVML spec per case; "init_error" makes nvmlInit_v2 fail.
CASES = {"complete": "complete.json", "planted": "planted.json", "nvml_error": "init_error"}

# Written by the fake NVML into nvml.json; as an extra identifier it must fail the leak check.
PLANTED_NAME = "OstiaFakeGPU-0001"

TOPO1 = re.compile(r"topo1:sha256:[0-9a-f]{64}")


def _env(nvml_dir: Path, data: Path, case: str, tmpdir: Path | None) -> dict[str, str]:
    env = dict(os.environ)
    env["HWLOC_XMLFILE"] = str(data / "hwloc-input.xml")
    previous = env.get("LD_LIBRARY_PATH")
    env["LD_LIBRARY_PATH"] = str(nvml_dir) + (os.pathsep + previous if previous else "")
    spec = CASES[case]
    env["OSTIA_FAKE_NVML"] = spec if spec == "init_error" else str(data / "fake-nvml" / spec)
    if tmpdir is not None:
        env["TMPDIR"] = str(tmpdir)
    return env


def _common(data: Path) -> list[str]:
    return ["--sysfs-root", str(data / "capture-root" / "sys"), "--no-verbs"]


def run_case(
    binary: Path, nvml_dir: Path, data: Path, case: str, out: Path, *, tmpdir: Path | None = None
) -> tuple[int, Path]:
    """Capture the fake machine into out for one of CASES; returns (exit code, out).

    "planted" also passes --extra-identifiers naming the GPU model the fake reports, so the leak
    check must find it. tmpdir, when given, becomes the capture's TMPDIR.
    """
    binary, nvml_dir, data, out = Path(binary), Path(nvml_dir), Path(data), Path(out)
    cmd = [str(binary), "--out", str(out), "--provider", "test", "--instance-type", "test"]
    cmd += _common(data)
    with tempfile.TemporaryDirectory() as scratch:
        if case == "planted":
            extra = Path(scratch) / "extra-identifiers.txt"
            extra.write_text(f"# planted by e2e.py\n{PLANTED_NAME}\n")
            cmd += ["--extra-identifiers", str(extra)]
        proc = subprocess.run(cmd, env=_env(nvml_dir, data, case, tmpdir), check=False)
    return proc.returncode, out


def run_print_id(
    binary: Path, nvml_dir: Path, data: Path, *, tmpdir: Path | None = None, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """--print-id on the complete fake machine; stdout holds only the id."""
    cmd = [str(binary), "--print-id", *_common(Path(data))]
    env = _env(Path(nvml_dir), Path(data), "complete", tmpdir)
    return subprocess.run(cmd, env=env, cwd=cwd, check=False, capture_output=True, text=True)


class Failure(Exception):
    pass


def _expect(ok: bool, message: str) -> None:
    if not ok:
        raise Failure(message)


def _manifest(out: Path) -> dict:
    return json.loads((out / "manifest.json").read_text())


def _names(directory: Path) -> set[str]:
    return {p.name for p in directory.iterdir()}


def _check_capture(args: argparse.Namespace, root: Path) -> None:
    for case, code, status in [
        ("complete", 0, "complete"),
        ("nvml_error", 2, "partial"),
        ("planted", 3, "failed"),
    ]:
        tmpdir = root / f"{case}-tmp"
        tmpdir.mkdir()
        out = root / case
        rc, out = run_case(args.binary, args.nvml_dir, args.data, case, out, tmpdir=tmpdir)
        _expect(rc == code, f"{case}: exit {rc}, expected {code}")
        manifest = _manifest(out)
        _expect(manifest["status"] == status, f"{case}: status {manifest['status']}")
        _expect(not any(tmpdir.iterdir()), f"{case}: the scratch directory was left in TMPDIR")
        listed = set(manifest["files"])
        if case == "planted":
            _expect(manifest["leak_check"] == "failed", f"{case}: leak_check not failed")
            _expect("leak" in manifest["errors"], f"{case}: no leak error")
            left = _names(out)
            _expect(left == {"manifest.json", "diagnostics.txt"}, f"{case}: left {sorted(left)}")
            _expect(not listed and manifest["topology_id"] is None, f"{case}: not the failed form")
            continue
        _expect(TOPO1.fullmatch(manifest["topology_id"] or "") is not None, f"{case}: no id")
        _expect(manifest["leak_check"] == "passed", f"{case}: leak_check not passed")
        expected = {"hwloc.xml", "nics.json", "meta.json"}
        if case == "complete":
            expected.add("nvml.json")
        _expect(listed == expected, f"{case}: manifest lists {sorted(listed)}")
        left = _names(out)
        _expect(left == expected | {"manifest.json", "diagnostics.txt"}, f"{case}: left {left}")
        nics = json.loads((out / "nics.json").read_text())
        _expect(nics["rdma_probe"] == "unavailable", f"{case}: verbs probe ran")
        if case == "nvml_error":
            missing = [m["file"] for m in manifest["missing"]]
            _expect(missing == ["nvml.json"], f"{case}: missing lists {missing}")
        else:
            gpus = json.loads((out / "nvml.json").read_text())["gpus"]
            _expect([g["bus_id"] for g in gpus] == ["0000:11:00.0"], f"{case}: GPU bus IDs")
            _expect(len(gpus[0]["nvlinks"]) == 2, f"{case}: NVLink count")
            _expect(gpus[0]["cuda_ordinal"] == 0, f"{case}: cuda_ordinal")


def _check_print_id(args: argparse.Namespace, root: Path) -> None:
    tmpdir = root / "print-id-tmp"
    tmpdir.mkdir()
    cwd = root / "print-id-cwd"
    cwd.mkdir()
    proc = run_print_id(args.binary, args.nvml_dir, args.data, tmpdir=tmpdir, cwd=cwd)
    _expect(proc.returncode == 0, f"--print-id: exit {proc.returncode}")
    lines = proc.stdout.splitlines()
    _expect(
        len(lines) == 1 and TOPO1.fullmatch(lines[0]) is not None,
        f"--print-id: {len(lines)} stdout line(s), expected one topo1 id",
    )
    _expect(not any(tmpdir.iterdir()), "--print-id: left files in TMPDIR")
    _expect(not any(cwd.iterdir()), "--print-id: left files in its working directory")


def _check_cli(args: argparse.Namespace) -> None:
    for flag in ("--help", "--version"):
        proc = subprocess.run([str(args.binary), flag], check=False, capture_output=True)
        _expect(proc.returncode == 0, f"{flag}: exit {proc.returncode}")
        _expect(bool(proc.stdout.strip()), f"{flag}: printed nothing")
    usage = [
        [],
        ["--print-id", "--out", "x"],
        ["--out", "x"],
        ["--print-id", "--node-index", "-1"],
        ["--print-id", "--bogus"],
    ]
    for argv in usage:
        proc = subprocess.run([str(args.binary), *argv], check=False, capture_output=True)
        _expect(proc.returncode == 1, f"usage case {usage.index(argv)}: exit {proc.returncode}")
        _expect(b"error:" in proc.stderr, f"usage case {usage.index(argv)}: no contract error")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--nvml-dir", type=Path, required=True)
    parser.add_argument("--data", type=Path, required=True)
    args = parser.parse_args(argv)
    # --print-id runs in its own working directory, so relative paths would no longer resolve.
    args.binary, args.nvml_dir, args.data = (
        p.resolve() for p in (args.binary, args.nvml_dir, args.data)
    )
    with tempfile.TemporaryDirectory(prefix="ostia-capture-e2e-") as tmp:
        root = Path(tmp)
        try:
            _check_cli(args)
            _check_capture(args, root)
            _check_print_id(args, root)
        except Failure as failure:
            print(f"capture_e2e: FAIL: {failure}", file=sys.stderr)
            return 1
    print("capture_e2e: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
