"""A fake `Kube`: an in-memory API server with a scripted timeline, recording every call.

It has the same methods as ostia_dev.remote.k8s.kube.Kube, so K8sBackend, preflight and
admin run against it unchanged. Time moves only through FakeClock.sleep, which fires the
timeline, so a 20-minute schedule timeout runs in milliseconds.
"""

from __future__ import annotations

import copy
import io
import secrets
import subprocess
import tarfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from fakes.common import artifact_files, control_files, tar_bytes

CLUSTER_SCOPED = {"namespace", "node", "clusterrole", "clusterrolebinding", "servicecidr"}


@dataclass
class Call:
    verb: str
    args: tuple
    stdin_size: int = 0


def pod_phase(phase: str, reason: str | None = None, message: str = ""):
    def act(k: FakeKube) -> None:
        for pod in k._objs("pod"):
            st = pod["status"]
            st["phase"] = phase
            if phase == "Running":
                pod["spec"]["nodeName"] = "node-1"
                st["containerStatuses"] = [{"name": "supervisor", "state": {"running": {}}}]
            if reason in ("OOMKilled", "Error", "Completed"):
                code = 137 if reason == "OOMKilled" else 1
                terminated = {"terminated": {"reason": reason, "exitCode": code}}
                st["containerStatuses"] = [{"name": "supervisor", "state": terminated}]
            elif reason == "Evicted":
                st.update(reason="Evicted", message=message or "ephemeral local storage exceeded")
            elif reason == "PreemptionByScheduler":
                st["conditions"] = [
                    {
                        "type": "DisruptionTarget",
                        "status": "True",
                        "reason": reason,
                        "message": message or "preempted",
                    }
                ]
        if reason == "DeadlineExceeded":
            for job in k._objs("job"):
                job.setdefault("status", {})["conditions"] = [
                    {"type": "Failed", "status": "True", "reason": "DeadlineExceeded"}
                ]

    return act


def event(reason: str, message: str, involved: str = "pod", type_: str = "Warning"):
    def act(k: FakeKube) -> None:
        target = (k._objs(involved) or [None])[-1]
        meta = target["metadata"] if target else {"name": "?", "uid": "?"}
        k.event_list.append(
            {
                "kind": "Event",
                "type": type_,
                "reason": reason,
                "message": message,
                "involvedObject": {
                    "kind": involved.capitalize(),
                    "name": meta["name"],
                    "uid": meta["uid"],
                },
                "lastTimestamp": k._stamp(),
            }
        )

    return act


def log(lines: list[str]):
    def act(k: FakeKube) -> None:
        ts = k._nanostamp()
        k.log_lines.extend((ts, line) for line in lines)

    return act


def drop_stream():
    def act(k: FakeKube) -> None:
        k.drops.append(len(k.log_lines))

    return act


def lose_connection(for_seconds: float | None = None):
    def act(k: FakeKube) -> None:
        k.lost_until = None if for_seconds is None else k.clock.monotonic() + for_seconds
        k.lost = True

    return act


def exec_result(argv_prefix: list[str], code: int, stdout: bytes = b""):
    def act(k: FakeKube) -> None:
        k.exec_results.append((list(argv_prefix), code, stdout))

    return act


def forbid(verb: str, resource: str):
    def act(k: FakeKube) -> None:
        k.forbidden.add((verb, resource))

    return act


class FakeKube:
    def __init__(
        self,
        clock,
        *,
        root: Path,
        namespace: str = "ostia-test",
        context: str = "c1",
        version: str = "version-skew-ok.json",
    ) -> None:
        self.clock = clock
        self.root = Path(root)
        self.namespace = namespace
        self.context = context
        self.store: dict[tuple[str, str | None, str], dict] = {}
        self.calls: list[Call] = []
        self.timeline: list[tuple[float, object]] = []
        self.event_list: list[dict] = []
        self.log_lines: list[tuple[str, str]] = []
        self.drops: list[int] = []
        self.exec_results: list[tuple[list[str], int, bytes]] = []
        self.forbidden: set[tuple[str, str]] = set()
        self.pending_deletes: dict[tuple, float] = {}
        self.gone_after: float | None = 0
        self.lost = False
        self.lost_until: float | None = None
        self.step_codes: dict[str, int] = {}
        self.junit: str | None = "pass.xml"
        self.version_fixture = version
        clock.on_advance(self._tick)

    def script(self, timeline: list[tuple[float, object]]) -> None:
        now = self.clock.monotonic()
        self.timeline.extend((now + at, act) for at, act in timeline)
        self.timeline.sort(key=lambda e: e[0])
        self._tick(now)

    def load(self, fixture: Path) -> None:
        """Put every object of a fixture file (a List or one object) into the store."""
        import json

        doc = json.loads(Path(fixture).read_text())
        for obj in doc.get("items", [doc]) if doc.get("kind") == "List" else [doc]:
            self.apply(obj, record=False)

    def pod_file(self, pod: str, path: str) -> Path:
        return self.root / pod / path.lstrip("/")

    def _tick(self, t: float) -> None:
        while self.timeline and self.timeline[0][0] <= t:
            _, act = self.timeline.pop(0)
            act(self)
        for key, due in list(self.pending_deletes.items()):
            if due <= t:
                del self.pending_deletes[key]
                self._remove(key)
        if self.lost and self.lost_until is not None and t >= self.lost_until:
            self.lost = False

    def _stamp(self) -> str:
        return self.clock.now().strftime("%Y-%m-%dT%H:%M:%SZ")

    def _nanostamp(self) -> str:
        return self.clock.now().strftime("%Y-%m-%dT%H:%M:%S.000000000Z")

    def _key(self, kind: str, name: str, namespaced: bool = True) -> tuple:
        kind = kind.lower()
        ns = None if kind in CLUSTER_SCOPED or not namespaced else self.namespace
        return (kind, ns, name)

    def _objs(self, kind: str) -> list[dict]:
        return [o for (k, _, _), o in self.store.items() if k == kind]

    def _record(self, verb: str, *args, stdin_size: int = 0) -> None:
        self.calls.append(Call(verb, args, stdin_size))
        if self.lost:
            from ostia_dev.remote.k8s.kube import LostConnection

            raise LostConnection(f"error: lost the connection to context {self.context}")

    def _check(self, verb: str, resource: str) -> None:
        if (verb, resource) in self.forbidden:
            from ostia_dev.remote.k8s.kube import forbidden_error

            raise forbidden_error(verb, resource, self.namespace, self.context)

    @staticmethod
    def _matches(obj: dict, selector: str | None) -> bool:
        if not selector:
            return True
        labels = obj.get("metadata", {}).get("labels", {})
        for term in selector.split(","):
            k, _, v = term.partition("=")
            if labels.get(k) != v:
                return False
        return True

    def _start_pod(self, job: dict) -> None:
        name = job["metadata"]["name"]
        tmpl = copy.deepcopy(job["spec"]["template"])
        labels = {**tmpl.get("metadata", {}).get("labels", {}), "job-name": name}
        pod = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": f"{name}-{secrets.token_hex(3)[:5]}",
                "labels": labels,
                "ownerReferences": [{"kind": "Job", "name": name, "uid": job["metadata"]["uid"]}],
            },
            "spec": tmpl["spec"],
            "status": {"phase": "Pending"},
        }
        self.apply(pod, record=False)
        (self.root / pod["metadata"]["name"] / "w" / ".ostia").mkdir(parents=True, exist_ok=True)

    def _remove(self, key: tuple) -> None:
        obj = self.store.pop(key, None)
        if obj is None:
            return
        uid = obj["metadata"]["uid"]
        for k2, o in list(self.store.items()):
            owners = o["metadata"].get("ownerReferences", [])
            if any(r.get("uid") == uid for r in owners):
                self._remove(k2)

    def version(self) -> dict:
        import json

        self._record("version")
        fixture = Path(__file__).resolve().parents[1] / "fixtures" / "kube" / self.version_fixture
        return json.loads(fixture.read_text())

    def get(self, kind: str, name: str, *, namespaced: bool = True) -> dict | None:
        self._record("get", kind, name)
        self._check("get", kind)
        obj = self.store.get(self._key(kind, name, namespaced))
        return copy.deepcopy(obj) if obj else None

    def list(self, kind: str, *, selector: str | None = None, namespaced: bool = True) -> list:
        self._record("list", kind, selector)
        self._check("list", kind)
        kind = kind.lower()
        return [copy.deepcopy(o) for o in self._objs(kind) if self._matches(o, selector)]

    def apply(self, obj: dict, *, record: bool = True) -> dict:
        if record:
            self._record("apply", obj.get("kind"), obj["metadata"]["name"])
            self._check("create", obj["kind"].lower() + "s")
        obj = copy.deepcopy(obj)
        key = self._key(
            obj["kind"], obj["metadata"]["name"], obj["metadata"].get("namespace", True) is not None
        )
        old = self.store.get(key)
        meta = obj["metadata"]
        meta["uid"] = old["metadata"]["uid"] if old else str(uuid.uuid4())
        meta["creationTimestamp"] = old["metadata"]["creationTimestamp"] if old else self._stamp()
        obj.setdefault("status", {} if obj["kind"] != "Pod" else {"phase": "Pending"})
        self.store[key] = obj
        if obj["kind"] == "Job" and not obj["spec"].get("suspend") and not self._job_pods(obj):
            self._start_pod(obj)
        return copy.deepcopy(obj)

    def create(self, obj: dict) -> dict:
        if self._key(obj["kind"], obj["metadata"]["name"]) in self.store:
            raise FileExistsError(obj["metadata"]["name"])
        return self.apply(obj)

    def _job_pods(self, job: dict) -> list[dict]:
        name = job["metadata"]["name"]
        return [p for p in self._objs("pod") if p["metadata"]["labels"].get("job-name") == name]

    def patch(self, kind: str, name: str, patch: dict) -> dict:
        self._record("patch", kind, name)
        self._check("patch", kind + "s")
        obj = self.store[self._key(kind, name)]

        def merge(a: dict, b: dict) -> None:
            for k, v in b.items():
                if isinstance(v, dict) and isinstance(a.get(k), dict):
                    merge(a[k], v)
                else:
                    a[k] = v

        merge(obj, patch)
        if obj["kind"] == "Job" and not obj["spec"].get("suspend") and not self._job_pods(obj):
            self._start_pod(obj)
        return copy.deepcopy(obj)

    def delete(
        self, kind: str, name: str | None = None, *, selector: str | None = None, wait: bool = False
    ) -> None:
        self._record("delete", kind, name or selector)
        self._check("delete", kind + "s")
        keys = [
            k
            for k, o in self.store.items()
            if k[0] == kind.lower() and (k[2] == name if name else self._matches(o, selector))
        ]
        for key in keys:
            if self.gone_after == 0:
                self._remove(key)
            elif self.gone_after is not None:
                self.pending_deletes[key] = self.clock.monotonic() + self.gone_after

    def events(self) -> list[dict]:
        self._record("events")
        return copy.deepcopy(self.event_list)

    def run_status(self, run_id: str) -> dict:
        self._record("run_status", run_id)
        sel = f"ostia.dev/run-id={run_id}"
        items = [o for o in [*self._objs("job"), *self._objs("pod")] if self._matches(o, sel)]
        return {"kind": "List", "items": copy.deepcopy(items)}

    def logs_follow(self, pod: str, *, since_time: str | None = None):
        self._record("logs_follow", pod, since_time)
        i = 0
        if since_time:
            while i < len(self.log_lines) and self.log_lines[i][0] < since_time:
                i += 1
        for _ in range(100_000):
            while i < len(self.log_lines):
                ts, line = self.log_lines[i]
                yield f"{ts} {line}"
                i += 1
                if self.drops and self.drops[0] <= i:
                    self.drops.pop(0)
                    return
            phase = next(
                (p["status"]["phase"] for p in self._objs("pod") if p["metadata"]["name"] == pod),
                "Gone",
            )
            if phase not in ("Pending", "Running"):
                return
            self.clock.sleep(1)

    def _scripted(self, argv: list[str]):
        for prefix, code, out in self.exec_results:
            if argv[: len(prefix)] == prefix:
                return code, out
        return None

    def exec_in(self, pod: str, argv: list[str], stdin) -> subprocess.CompletedProcess:
        data = stdin.read() if stdin is not None else b""
        self._record("exec_in", pod, tuple(argv), stdin_size=len(data))
        self._check("create", "pods/exec")
        if (hit := self._scripted(argv)) is not None:
            return subprocess.CompletedProcess(argv, hit[0], b"", b"")
        if argv[:2] == ["tar", "-x"]:
            with tarfile.open(fileobj=io.BytesIO(data)) as t:
                t.extractall(self.root / pod / "w", filter="data")
        elif argv[0] == "touch":
            f = self.pod_file(pod, argv[1])
            f.parent.mkdir(parents=True, exist_ok=True)
            f.touch()
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    def exec_out(self, pod: str, argv: list[str], stdout=None) -> subprocess.CompletedProcess:
        self._record("exec_out", pod, tuple(argv))
        self._check("create", "pods/exec")
        code, out = 0, b""
        if (hit := self._scripted(argv)) is not None:
            code, out = hit
        elif argv[:2] == ["sh", "-c"] and argv[2].startswith("find"):
            w = self.root / pod / "w"
            n = sum(
                1
                for p in w.rglob("*")
                if p.is_file() and p.relative_to(w).parts[0] not in (".ostia", "home", "cache")
            )
            out = f"{n}\n".encode()
        elif argv[:2] == ["sh", "-c"] and ".ostia && tar" in argv[2]:
            out = tar_bytes(
                control_files(
                    self._env(pod, "OSTIA_PLAN"),
                    self.step_codes,
                    [line + "\n" for _, line in self.log_lines],
                )
            )
        elif argv[:2] == ["sh", "-c"] and "tar -cf" in argv[2]:
            out = tar_bytes(artifact_files(self._env(pod, "OSTIA_BUILD_DIR"), self.junit))
        if stdout is not None:
            stdout.write(out)
            return subprocess.CompletedProcess(argv, code, None, b"")
        return subprocess.CompletedProcess(argv, code, out, b"")

    def _env(self, pod: str, name: str) -> str:
        (p,) = [o for o in self._objs("pod") if o["metadata"]["name"] == pod]
        env = p["spec"]["containers"][0].get("env", [])
        return next(e["value"] for e in env if e["name"] == name)

    def can_i(self, verb: str, resource: str) -> bool:
        self._record("can_i", verb, resource)
        return (verb, resource) not in self.forbidden
