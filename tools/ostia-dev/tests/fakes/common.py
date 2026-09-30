"""Helpers the fake engine and the fake kube share."""

import io
import json
import tarfile
from pathlib import Path

JUNIT = Path(__file__).resolve().parents[1] / "fixtures" / "junit"


def tar_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def steps_for(plan: str, codes: dict[str, int] | None = None) -> dict:
    """The steps.json a supervisor would write for this OSTIA_PLAN."""
    steps = []
    for line in plan.splitlines():
        name, kind, _ = line.split("\t", 2)
        code = (codes or {}).get(name, 0)
        steps.append({"name": name, "kind": kind, "code": code, "seconds": 1,
                      "result": "ok" if code == 0 else "failed"})  # fmt: skip
    return {"schema": 1, "state": "done", "code_wait": "ok", "exit": 0, "steps": steps}


def control_files(plan: str, codes: dict[str, int], log_lines: list[str]) -> dict[str, bytes]:
    return {
        "steps.json": json.dumps(steps_for(plan, codes)).encode(),
        "log.txt": "".join(log_lines).encode(),
    }


def artifact_files(build_dir: str, junit: str | None) -> dict[str, bytes]:
    build = build_dir.removeprefix("/w/")
    files = {f"{build}/ostia-summary.txt": b"architectures: x\n"}
    if junit:
        files[f"{build}/junit.xml"] = (JUNIT / junit).read_bytes()
    return files
