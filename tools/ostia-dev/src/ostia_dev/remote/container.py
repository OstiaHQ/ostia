"""The `container` backend (RFC-0005 §5): the pipeline in a local podman or docker container."""

import datetime
import platform
import shutil
import subprocess
import sys
import tarfile
import time
from importlib import resources
from pathlib import Path

from ostia_dev import proc
from ostia_dev.contract import violation
from ostia_dev.errors import InfraError, UsageError
from ostia_dev.remote import results, suites
from ostia_dev.remote.core import Collected, Run, owner_id

CACHE_VOLUME = "ostia-pixi-cache"
# Outside HOME: a mount under /w/home would make the engine create HOME owned by root.
CACHE_DIR = f"{suites.WORK}/cache/rattler"
USER = "1000:1000"
FINISHED = "[ostia] finished with exit"
# Docker creates the volumes owned by root: chown them, then drop to uid 1000.
ROOT_INIT = (
    f"mkdir -p {CACHE_DIR} && chown 1000:1000 /w /w/cache {CACHE_DIR} && "
    'exec setpriv --reuid=1000 --regid=1000 --clear-groups /bin/sh -c "$1" ostia-supervisor'
)


def supervisor_script() -> str:
    return (resources.files("ostia_dev.remote") / "pod" / "supervisor.sh").read_text()


def detect_engine(requested: str | None, which=shutil.which) -> str:
    for name in [requested] if requested else ["podman", "docker"]:
        if path := which(name):
            return path
    wanted = requested or "podman or docker"
    raise UsageError(
        violation(
            f"remote container needs {wanted}, and it was not found on PATH",
            [],
            "the container backend runs the pipeline in a local podman or docker container",
            "install podman (brew install podman && podman machine init && podman machine "
            "start) or Docker Desktop",
            "RFC-0005 §5",
        )
    )


def host_arch() -> str:
    return {"x86_64": "amd64", "amd64": "amd64", "arm64": "arm64", "aarch64": "arm64"}[
        platform.machine().lower()
    ]


class ContainerBackend:
    name = "container"

    def __init__(self, engine: str | None = None, *, gpus: bool = False, runner=proc,
                 which=shutil.which, arch: str | None = None) -> None:  # fmt: skip
        self.requested = engine
        self.gpus = gpus
        self.runner = runner
        self.which = which
        self.arch = arch
        self.engine = ""

    @property
    def kind(self) -> str:
        return Path(self.engine).name

    def _run(self, *args: str, **kw) -> subprocess.CompletedProcess:
        return self.runner.run([self.engine, *args], **kw)

    def _exec(self, run: Run, *argv: str, stdin=None, **kw) -> subprocess.CompletedProcess:
        interactive = ["-i"] if stdin is not None else []
        return self._run("exec", *interactive, "--user", USER, run.state["name"], *argv,
                         stdin=stdin, **kw)  # fmt: skip

    def prepare(self, run: Run) -> None:
        self.engine = detect_engine(self.requested, self.which)
        if self.gpus and not run.profile.is_gpu:
            raise UsageError(self._gpu_mismatch(run, "--gpus needs a GPU profile"))
        if run.profile.is_gpu and not self.gpus:
            raise UsageError(self._gpu_mismatch(run, f"profile {run.profile.name} needs --gpus"))
        run.state["name"] = f"ostia-{run.run_id}"

    def _gpu_mismatch(self, run: Run, problem: str) -> str:
        return violation(
            problem,
            [f"profile: {run.profile.name} (kind {run.profile.kind})"],
            "a container run passes the host's GPUs only with --gpus, and a GPU profile "
            "(for its compute capability and GPU checks) goes with it",
            "pass --gpus --profile <gpu profile, e.g. l4>, or neither",
            "RFC-0005 §5",
        )

    def gc(self, run: Run) -> list[str]:
        r = self._run(
            "ps", "-a",
            "--filter", "label=ostia.dev/managed=true",
            "--filter", f"label=ostia.dev/owner={owner_id()}",
            "--format", "{{.Names}}",
        )  # fmt: skip
        removed = []
        now = datetime.datetime.now(datetime.UTC)
        for name in r.stdout.split() if r.returncode == 0 else []:
            expires = self._run(
                "inspect", "--format", '{{index .Config.Labels "ostia.dev/expires"}}', name
            ).stdout.strip()
            try:
                when = datetime.datetime.strptime(expires, "%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                continue
            if when.replace(tzinfo=datetime.UTC) < now:
                self._run("rm", "-f", "-v", name)
                removed.append(name)
        return removed

    def start_argv(self, run: Run) -> list[str]:
        w = run.windows
        expires = datetime.datetime.now(datetime.UTC) + datetime.timedelta(
            seconds=w["code_wait"] + w["timeout"] + w["collect"] + w["margin"]
        )
        podman = self.kind == "podman"
        argv = [
            "run", "-d", "--name", run.state["name"],
            "--label", "ostia.dev/managed=true",
            "--label", f"ostia.dev/run-id={run.run_id}",
            "--label", f"ostia.dev/owner={owner_id()}",
            "--label", f"ostia.dev/expires={expires:%Y-%m-%dT%H:%M:%SZ}",
            "--init",
            "--platform", f"linux/{self.arch or host_arch()}",
            "--cap-drop", "ALL",
        ]  # fmt: skip
        if podman:
            argv += ["--user", USER]
        else:  # for the root init step; setpriv drops them before the supervisor runs
            argv += ["--cap-add", "CHOWN", "--cap-add", "SETUID", "--cap-add", "SETGID"]
        argv += ["--security-opt", "no-new-privileges"]
        for key, value in {**run.supervisor_env(), "RATTLER_CACHE_DIR": CACHE_DIR}.items():
            argv += ["-e", f"{key}={value}"]
        argv += ["-v", suites.WORK, "-v", f"{CACHE_VOLUME}:{CACHE_DIR}{':U' if podman else ''}"]
        if self.gpus:
            argv += ["--device", "nvidia.com/gpu=all"] if podman else ["--gpus", "all"]
        argv.append(run.image)
        script = supervisor_script()
        if podman:
            argv += ["/bin/sh", "-c", script, "ostia-supervisor"]
        else:
            argv += ["/bin/sh", "-c", ROOT_INIT, "ostia-init", script]
        return argv

    def start(self, run: Run) -> None:
        t0 = time.monotonic()
        r = self._run(*self.start_argv(run))
        if r.returncode != 0:
            raise InfraError(
                violation(
                    f"{self.kind} could not start the container",
                    [line for line in r.stderr.strip().splitlines()[-5:]],
                    "the container backend needs a working engine and the pinned image",
                    f"check `{self.kind} info`, and that the image can be pulled: {run.image}",
                    "RFC-0005 §5",
                ),
                step="start",
            )
        run.phases["start"] = round(time.monotonic() - t0)

    def upload(self, run: Run) -> None:
        t0 = time.monotonic()
        with run.tarball.path.open("rb") as f:
            r = self._exec(run, "tar", "-x", "-C", suites.WORK, stdin=f)
        if r.returncode != 0:
            raise self._upload_failed(run, f"tar -x exited {r.returncode}: {r.stderr.strip()}")
        count = self._exec(
            run, "sh", "-c",
            f"find {suites.WORK} -type f ! -path '{suites.WORK}/.ostia/*' "
            f"! -path '{suites.WORK}/home/*' ! -path '{suites.WORK}/cache/*' | wc -l",
        )  # fmt: skip
        got = count.stdout.strip()
        if count.returncode != 0 or got != str(run.tarball.file_count):
            raise self._upload_failed(
                run, f"{got or '?'} files arrived, the tarball has {run.tarball.file_count}"
            )
        if self._exec(run, "touch", f"{suites.WORK}/.ostia/ready").returncode != 0:
            raise self._upload_failed(run, "could not write the ready marker")
        run.phases["upload"] = round(time.monotonic() - t0)

    def _upload_failed(self, run: Run, detail: str) -> InfraError:
        return InfraError(
            violation(
                "the upload into the container failed",
                [detail, f"container: {run.state['name']}"],
                "the code must arrive complete before the pipeline starts (RFC-0005 §4.10)",
                "a dropped engine connection or a full disk: check `df` in the podman "
                "machine or Docker VM (ephemeral_storage on a cluster), then rerun",
                "RFC-0005 §4.10",
            ),
            step="upload",
        )

    def stream(self, run: Run) -> None:
        p = self.runner.stream([self.engine, "logs", "-f", run.state["name"]])
        try:
            for line in p.stdout:
                sys.stdout.write(line)
                sys.stdout.flush()
                if line.startswith(FINISHED):
                    break
        finally:
            p.terminate()
            p.wait()
        state = self._run(
            "inspect", "--format", "{{.State.Running}} {{.State.OOMKilled}}", run.state["name"]
        ).stdout.split()
        run.state["running"] = bool(state) and state[0] == "true"
        run.oom = len(state) > 1 and state[1] == "true"

    def collect(self, run: Run, workdir: Path) -> list[Collected]:
        control, artifacts = workdir / "control.tar", workdir / "artifacts.tar"
        name = run.state["name"]
        build_rel = run.plan.build_dir.removeprefix(suites.WORK + "/")
        if run.state.get("running"):
            self._to_file(control, "exec", "--user", USER, name, "sh", "-c",
                          results.control_tar_script())  # fmt: skip
            self._to_file(artifacts, "exec", "--user", USER, name, "sh", "-c",
                          results.artifact_tar_script(build_rel))  # fmt: skip
            self._exec(run, "touch", f"{suites.WORK}/.ostia/collected")
        else:  # exec needs a running container; cp still reads a stopped one
            self._to_file(control, "cp", f"{name}:{suites.WORK}/.ostia", "-")
            with tarfile.open(artifacts, "w"):
                pass
        return [Collected("", control, artifacts)]

    def _to_file(self, path: Path, *args: str) -> None:
        with path.open("wb") as f:
            r = self.runner.run([self.engine, *args], capture=False, stdout=f,
                                stderr=subprocess.PIPE)  # fmt: skip
        if r.returncode != 0:
            raise InfraError(
                violation(
                    "could not copy the results out of the container",
                    [f"{self.kind} {args[0]}: {(r.stderr or '').strip()}"],
                    "results are copied back before the container is removed (RFC-0005 §3.4)",
                    "rerun; if it repeats, check the engine with -v",
                    "RFC-0005 §3.4",
                ),
                step="collect",
            )

    def teardown(self, run: Run) -> bool:
        name = run.state.get("name")
        if not name or not self.engine:
            return True
        self._run("rm", "-f", "-v", name)
        for _ in range(10):
            if self._run("inspect", name).returncode != 0:
                return True
            time.sleep(0.5)
        return False

    def cleanup_hint(self, run: Run) -> str:
        return f"{self.kind or 'podman'} rm -f -v {run.state.get('name', 'ostia-' + run.run_id)}"

    def describe(self, run: Run) -> dict:
        return {"engine": self.kind, "image": run.image, "gpus": self.gpus}
