"""A fake podman/docker: records every argv and simulates one container's /w in a directory.

It stands in for ostia_dev.proc (run and stream), so ContainerBackend runs unchanged.
"""

import io
import subprocess
import tarfile
from pathlib import Path

from fakes.common import artifact_files, control_files, tar_bytes

FINISHED = "[ostia] finished with exit 0; waiting for the results to be collected\n"


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
            return self._write(cmd, kw, tar_bytes(files))
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
        env = self.containers[name]["env"]
        return control_files(env["OSTIA_PLAN"], self.step_codes, self.log_lines)

    def _control(self, name) -> bytes:
        return tar_bytes(self._control_files(name))

    def _artifacts(self, name) -> bytes:
        return tar_bytes(
            artifact_files(self.containers[name]["env"]["OSTIA_BUILD_DIR"], self.junit)
        )

    def container(self) -> dict:
        (c,) = self.containers.values()
        return c
