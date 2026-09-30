"""A fake podman/docker: records every argv and simulates one container's /w in a directory.

It stands in for ostia_dev.proc (run and stream), so ContainerBackend runs unchanged.
"""

import io
import json
import subprocess
import tarfile
from pathlib import Path

JUNIT = Path(__file__).resolve().parents[1] / "fixtures" / "junit"
FINISHED = "[ostia] finished with exit 0; waiting for the results to be collected\n"


def _steps(plan: str) -> dict:
    steps = []
    for line in plan.splitlines():
        name, kind, _ = line.split("\t", 2)
        steps.append({"name": name, "kind": kind, "code": 0, "seconds": 1, "result": "ok"})
    return {"schema": 1, "state": "done", "code_wait": "ok", "exit": 0, "steps": steps}


def _tar(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class _Stream:
    def __init__(self, lines: list[str], on_read=None):
        self.stdout = iter(lines)
        self.terminated = False
        self._on_read = on_read

    def terminate(self):
        self.terminated = True

    def wait(self):
        return 0


class FakeEngine:
    def __init__(self, root: Path):
        self.root = root
        self.calls: list[list[str]] = []
        self.containers: dict[str, dict] = {}
        self.fail: dict[str, int] = {}  # "tar-x", "run", "count", "collect"
        self.raise_in: dict[str, BaseException] = {}  # "stream", "upload"
        self.step_codes: dict[str, int] = {}  # step name -> code in steps.json
        self.junit = "pass.xml"
        self.oom = False
        self.running_after_stream = True
        self.keep_after_rm = False
        self.expired: list[str] = []  # names that ps lists, all expired
        self.log_lines = ["[ostia] step install (install): pixi install\n"]

    def which(self, name):
        return f"/usr/bin/{name}" if name in ("podman", "docker") else None

    def run(self, cmd, *, input=None, capture=True, check=False, verbose=None, **kw):
        cmd = [str(c) for c in cmd]
        self.calls.append(cmd)
        sub = cmd[1]
        out, rc = "", 0
        if sub == "run":
            if "run" in self.fail:
                return subprocess.CompletedProcess(cmd, self.fail["run"], "", "pull failed")
            name = cmd[cmd.index("--name") + 1]
            env = {}
            for i, a in enumerate(cmd):
                if a == "-e":
                    k, _, v = cmd[i + 1].partition("=")
                    env[k] = v
            (self.root / name / "w" / ".ostia").mkdir(parents=True)
            self.containers[name] = {"argv": cmd, "env": env, "removed": False}
        elif sub == "exec":
            name = cmd[cmd.index("--user") + 2]
            argv = cmd[cmd.index("--user") + 3 :]
            w = self.root / name / "w"
            if argv[:2] == ["tar", "-x"]:
                if "-i" not in cmd[: cmd.index("--user")]:  # without -i no stdin reaches tar
                    return subprocess.CompletedProcess(cmd, 2, "", "not a tar archive")
                if "upload" in self.raise_in:
                    raise self.raise_in.pop("upload")
                rc = self.fail.get("tar-x", 0)
                if rc == 0:
                    with tarfile.open(fileobj=io.BytesIO(kw["stdin"].read())) as t:
                        t.extractall(w, filter="data")
            elif argv[:2] == ["sh", "-c"] and argv[2].startswith("find"):
                n = sum(1 for p in w.rglob("*") if p.is_file() and ".ostia" not in p.parts
                        and p.relative_to(w).parts[0] not in ("home", "cache"))  # fmt: skip
                out = str(n + self.fail.get("count", 0))
            elif argv[0] == "touch":
                (self.root / name / argv[1].lstrip("/")).touch()
            elif argv[:2] == ["sh", "-c"] and ".ostia && tar" in argv[2]:
                return self._write(cmd, kw, self._control(name))
            elif argv[:2] == ["sh", "-c"] and "tar -cf" in argv[2]:
                return self._write(cmd, kw, self._artifacts(name))
        elif sub == "cp":
            name = cmd[2].split(":")[0]
            files = {f".ostia/{k}": v for k, v in self._control_files(name).items()}
            return self._write(cmd, kw, _tar(files))
        elif sub == "inspect":
            name = cmd[-1]
            c = self.containers.get(name)
            if "--format" in cmd and "State" in cmd[cmd.index("--format") + 1]:
                out = f"{str(self.running_after_stream).lower()} {str(self.oom).lower()}"
            elif "--format" in cmd:
                out = "2000-01-01T00:00:00Z" if name in self.expired else "2999-01-01T00:00:00Z"
            elif not c or (c["removed"] and not self.keep_after_rm):
                rc = 1
        elif sub == "rm":
            name = cmd[-1]
            if name in self.containers:
                self.containers[name]["removed"] = True
        elif sub == "ps":
            out = "\n".join(self.expired + ["ostia-fresh"])
        return subprocess.CompletedProcess(cmd, rc, out, "")

    def stream(self, cmd, *, verbose=None, **kw):
        self.calls.append([str(c) for c in cmd])
        if "stream" in self.raise_in:
            raise self.raise_in["stream"]
        return _Stream([*self.log_lines, FINISHED])

    def _write(self, cmd, kw, data: bytes):
        rc = self.fail.get("collect", 0)
        if rc == 0:
            kw["stdout"].write(data)
        return subprocess.CompletedProcess(cmd, rc, None, "")

    def _control_files(self, name) -> dict[str, bytes]:
        steps = _steps(self.containers[name]["env"]["OSTIA_PLAN"])
        for s in steps["steps"]:
            if s["name"] in self.step_codes:
                s["code"] = self.step_codes[s["name"]]
                s["result"] = "failed"
        return {"steps.json": json.dumps(steps).encode(), "log.txt": b"".join(
            line.encode() for line in self.log_lines)}  # fmt: skip

    def _control(self, name) -> bytes:
        return _tar(self._control_files(name))

    def _artifacts(self, name) -> bytes:
        build = self.containers[name]["env"]["OSTIA_BUILD_DIR"].removeprefix("/w/")
        files = {f"{build}/ostia-summary.txt": b"architectures: x\n"}
        if self.junit:
            files[f"{build}/junit.xml"] = (JUNIT / self.junit).read_bytes()
        return _tar(files)

    def container(self) -> dict:
        (c,) = self.containers.values()
        return c
