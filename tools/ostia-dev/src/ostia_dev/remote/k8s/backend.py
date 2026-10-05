"""The `k8s` backend's single-pod lifecycle (RFC-0005 §4.2, §4.5, §4.8).

CLI (Mac)                               pod (supervisor.sh)
gc: delete this owner's expired Jobs
create Job (suspend) -> Secret (owned) -> unsuspend
poll every 3 s (run_status + events)    wait for .ostia/ready
exec -i tar -x, count files, touch ready -> install, build, command
logs -f --timestamps, resumed, deduped  <- output
exec tar -c control files, artifacts
capture: test manifest, wc, tar (capture.fetch; RFC-0003 §4)
touch collected (not when kept)         -> exit with the command's code
delete Job (cascades), poll until gone
"""

import datetime
import fnmatch
import io
import itertools
import json
import signal
import sys
import tarfile
from pathlib import Path

from ostia_dev.clock import Clock
from ostia_dev.config import Config
from ostia_dev.contract import violation
from ostia_dev.errors import InfraError, OstiaError, UsageError
from ostia_dev.remote import capture, results, suites
from ostia_dev.remote.container import supervisor_script
from ostia_dev.remote.core import SECRET_KEYS, Collected, Run, owner_id
from ostia_dev.remote.k8s import manifests, preflight
from ostia_dev.remote.k8s.kube import KubeError, LostConnection
from ostia_dev.remote.k8s.preflight import Target

FINISHED = "[ostia] finished with exit"
POLL = 3
LOST_FOR_GOOD = 120
TEARDOWN_WAIT = 60
PULL_ERRORS = ("ErrImagePull", "ImagePullBackOff", "InvalidImageName")
CACHE_ERRORS = ("volume node affinity conflict", "Multi-Attach error", "had volume")


def _secret_keys(env_vars: dict[str, str]) -> set[str]:
    return {k for k in env_vars if any(fnmatch.fnmatchcase(k.upper(), p) for p in SECRET_KEYS)}


def _parse_stamp(text: str) -> datetime.datetime | None:
    try:
        return datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.UTC)
    except (TypeError, ValueError):
        return None


class K8sBackend:
    name = "k8s"

    def __init__(
        self, *, cfg: Config, target: Target, kube, clock: Clock | None = None, err=None
    ) -> None:
        self.cfg = cfg
        self.target = target
        self.kube = kube
        self.clock = clock or Clock()
        self.err = err or sys.stderr
        self.notes: list[str] = []

    def _say(self, text: str) -> None:
        print(f"[ostia] {text}", file=self.err, flush=True)

    def _infra(
        self,
        run: Run,
        problem: str,
        details: list[str],
        fix: str,
        *,
        step: str | None,
        see: str = "RFC-0005 §4.5",
    ) -> InfraError:
        return InfraError(
            violation(
                problem,
                [f"run: {run.run_id}", *details],
                "an infrastructure failure leaves the test result unknown",
                fix,
                see,
            ),
            step=step,
        )

    def prepare(self, run: Run) -> None:
        self.notes = preflight.check(
            self.target, self.kube, run.profile, yes=run.spec.yes, cfg=self.cfg
        )
        run.state.update(
            job=manifests.job_name(run.run_id),
            keep=run.windows.get("keep", 0),
            cache=bool(run.spec.extra.get("cache")),
            n=int(run.spec.extra.get("pods") or 1),
            same_node=bool(run.spec.extra.get("same_node")),
            secret_keys=_secret_keys(run.env_vars) if run.spec.allow_secret else set(),
            capture_env=self._capture_env(run),
        )

    def _capture_env(self, run: Run) -> dict[str, str]:
        """The values topo capture defaults from in the pod (RFC-0003 §1, §3): the context and
        cluster names are identifiers the leak check must find if the pod's files hold them."""
        try:
            cluster = self.kube.cluster_name()
        except OstiaError:
            cluster = None
            self.notes.append("the kubeconfig cluster name could not be read for the leak check")
        return {
            "OSTIA_CAPTURE_PROVIDER": preflight.capture_provider(self.target.provider),
            "OSTIA_CAPTURE_INSTANCE_TYPE": preflight.instance_type(self.target.nodes, run.profile),
            # topo capture splits this on newlines only, so a value may hold spaces
            "OSTIA_LEAK_IDENTIFIERS": "\n".join(
                capture.leak_identifiers([self.target.context, cluster])
            ),
        }

    def gc(self, run: Run) -> list[str]:
        now = self.clock.now()
        removed = []
        jobs = self.kube.list(
            "job", selector=f"ostia.dev/managed=true,ostia.dev/owner={owner_id()}"
        )
        for job in jobs:
            expires = _parse_stamp(job["metadata"].get("annotations", {}).get("ostia.dev/expires"))
            if expires and expires < now:
                self.kube.delete("job", job["metadata"]["name"])
                removed.append(job["metadata"]["name"])
        return removed

    def start(self, run: Run) -> None:
        state = run.state
        ns = self.target.namespace
        if state["cache"] and not self.kube.get("persistentvolumeclaim", manifests.CACHE_PVC):
            size = self.cfg.context(self.target.context).get("cache_size", "100Gi")
            self.kube.apply(manifests.pvc(ns, size, owner_id()))
        secret = f"{state['job']}-env" if state["secret_keys"] else None
        job = manifests.job(
            run,
            namespace=ns,
            script=supervisor_script(),
            owner=owner_id(),
            now=self.clock.now(),
            keep=state["keep"],
            cache=state["cache"],
            secret=secret,
            secret_keys=state["secret_keys"],
            pods=state["n"],
            same_node=state["same_node"],
        )
        state["created"] = True  # before the call: the server may create it and the reply be lost
        created = self.kube.apply(job)
        state["job_uid"] = created["metadata"]["uid"]
        if secret:
            values = {k: run.env_vars[k] for k in state["secret_keys"]}
            ref = manifests.owner_reference(created)
            # create, not apply: apply copies stringData into last-applied-configuration
            self.kube.create(manifests.secret(run.run_id, values, ref, owner_id()))
        if state["n"] > 1:  # owned objects first, so no pod runs before its policy (§4.2)
            ref = manifests.owner_reference(created)
            self.kube.create(manifests.service(run.run_id, state["job"], ref, owner_id()))
            # In an unguarded namespace nothing restricts the pods; a policy selecting them
            # would become their only egress rule and cut DNS and the internet.
            if self.kube.list("networkpolicy", selector="ostia.dev/managed=true"):
                self.kube.create(manifests.run_policy(run.run_id, ref, owner_id()))
        self.kube.patch("job", state["job"], {"spec": {"suspend": False}})
        self._wait_running(run)

    def _pods(self, run: Run, items: list[dict]) -> list[dict]:
        """The run's pods in rank order (the Indexed Job's completion index)."""
        pods = [i for i in items if i.get("kind") == "Pod"]
        if run.state["n"] == 1:
            return pods[-1:]
        return sorted(pods, key=self._index)

    @staticmethod
    def _index(pod: dict) -> int:
        return int(pod["metadata"].get("annotations", {}).get(manifests.INDEX, 0))

    def _who(self, run: Run, pod: dict | None) -> str:
        return f"rank {self._index(pod)}: " if run.state["n"] > 1 and pod else ""

    def _wait_running(self, run: Run) -> None:
        t0 = self.clock.monotonic()
        limit = run.windows["schedule_timeout"]
        last_scheduling, shown = None, set()
        while True:
            items = self.kube.run_status(run.run_id).get("items", [])
            uids = {run.state["job_uid"], *(i["metadata"]["uid"] for i in items)}
            events = [e for e in self.kube.events() if e["involvedObject"].get("uid") in uids]
            for e in events:
                reason, message = e.get("reason", ""), e.get("message", "")
                if (reason, message) not in shown:
                    shown.add((reason, message))
                    self._say(f"{round(self.clock.monotonic() - t0)}s {reason}: {message}")
                if reason == "FailedCreate":
                    raise UsageError(
                        violation(
                            "the cluster refused to create the run's pod",
                            [message],
                            "admission (quota, Pod Security, webhooks) runs before a pod exists",
                            "fix what the message names, or raise the namespace's quota with init",
                            "RFC-0005 §4.2",
                        ),
                        step="start",
                    )
                if reason == "FailedScheduling":
                    last_scheduling = message
                # A zone conflict shows as FailedScheduling, a volume in use as FailedAttachVolume
                if run.state["cache"] and any(c in message for c in CACHE_ERRORS):
                    raise self._infra(
                        run,
                        "the --cache volume can't be used on this node",
                        [message, f"volume: {manifests.CACHE_PVC}"],
                        "rerun without --cache, or remove the volume with "
                        "ostia-dev remote k8s cleanup --cache",
                        step="start",
                        see="RFC-0005 §4.7",
                    )
                if any(p in message for p in PULL_ERRORS):
                    raise self._infra(
                        run,
                        f"the image could not be pulled: {run.image}",
                        [message],
                        "check the image and the node's egress",
                        step="start",
                    )
            pods = self._pods(run, items)
            for pod in pods:
                for cs in pod.get("status", {}).get("containerStatuses", []):
                    reason = cs.get("state", {}).get("waiting", {}).get("reason", "")
                    if reason in PULL_ERRORS:
                        raise self._infra(
                            run,
                            f"{self._who(run, pod)}the image could not be pulled: {run.image}",
                            [reason],
                            "check the image",
                            step="start",
                        )
            dead = [p for p in pods if p.get("status", {}).get("phase") in ("Succeeded", "Failed")]
            if dead:
                self._terminal(run, self._culprit(dead), items, step="start")
            running = [p for p in pods if p.get("status", {}).get("phase") == "Running"]
            if pods and len(running) == run.state["n"]:
                run.state["pods"] = [p["metadata"]["name"] for p in running]
                run.state["pod"] = run.state["pods"][0]
                if run.state["n"] == 1:
                    run.node = running[0]["spec"].get("nodeName")
                else:
                    run.ranks = [
                        {"rank": i, "pod": p["metadata"]["name"], "node": p["spec"].get("nodeName")}
                        for i, p in enumerate(running)
                    ]
                run.phases["node"] = round(self.clock.monotonic() - t0)
                return
            if self.clock.monotonic() - t0 >= limit:
                waiting = [f"rank {self._index(p)}" for p in pods if p not in running] or [
                    "the pod"
                ]
                who = "the pod" if run.state["n"] == 1 else ", ".join(waiting)
                raise self._infra(
                    run,
                    f"no node took {who} within --schedule-timeout ({limit // 60} min)",
                    [f"last FailedScheduling: {last_scheduling or '(none)'}"],
                    f"check [remote.k8s.profiles.{run.profile.name}] (node_selector, "
                    "tolerations, resources), or raise --schedule-timeout",
                    step="start",
                )
            self.clock.sleep(POLL)

    def upload(self, run: Run) -> None:
        """Only once every pod is Running (§4.11), and ready only once every copy is checked."""
        t0 = self.clock.monotonic()
        for pod in run.state["pods"]:
            self._upload_one(run, pod)
        for pod in run.state["pods"]:
            self.kube.exec_out(pod, ["touch", f"{suites.WORK}/.ostia/ready"])
        run.phases["upload"] = round(self.clock.monotonic() - t0)

    def _upload_one(self, run: Run, pod: str) -> None:
        with run.tarball.path.open("rb") as f:
            r = self.kube.exec_in(pod, ["tar", "-x", "-C", suites.WORK], f)
        if r.returncode != 0:
            raise self._upload_failed(run, f"tar -x exited {r.returncode}")
        count = self.kube.exec_out(
            pod,
            [
                "sh",
                "-c",
                f"find {suites.WORK} -type f ! -path '{suites.WORK}/.ostia/*' "
                f"! -path '{suites.WORK}/home/*' ! -path '{suites.WORK}/cache/*' | wc -l",
            ],
        )
        got = (count.stdout or b"").decode().strip()
        if got != str(run.tarball.file_count):
            raise self._upload_failed(
                run, f"{got or '?'} files arrived, the tarball has {run.tarball.file_count}"
            )

    def _upload_failed(self, run: Run, detail: str) -> InfraError:
        return self._infra(
            run,
            "the upload into the pod failed",
            [detail],
            "a dropped connection, or /w is full: raise ephemeral_storage in "
            f"[remote.k8s.profiles.{run.profile.name}], then rerun",
            step="upload",
            see="RFC-0005 §4.10",
        )

    def stream(self, run: Run) -> None:
        if run.state["n"] > 1:
            return self._poll_ranks(run)
        last_ts, at_last, lost_since = None, 0, None
        run.state["last_step"] = None
        while True:
            skip = at_last
            try:
                for raw in self.kube.logs_follow(run.state["pod"], since_time=last_ts):
                    ts, _, line = raw.partition(" ")
                    if last_ts and ts < last_ts:
                        continue
                    if ts == last_ts and skip:
                        skip -= 1
                        continue
                    if ts != last_ts:
                        last_ts, at_last = ts, 0
                    at_last += 1
                    lost_since = None
                    print(line, flush=True)
                    if line.startswith("[ostia] step ") and " (" in line:
                        run.state["last_step"] = line.split()[2]
                    if line.startswith(FINISHED):
                        return
                lost_since = None
                items = self.kube.run_status(run.run_id).get("items", [])
            except LostConnection:
                lost_since = self._lost(run, lost_since)
                self.clock.sleep(POLL)
                continue
            pod = next(iter(self._pods(run, items)), None)
            if pod is None or pod["status"].get("phase") not in ("Running", "Pending"):
                self._terminal(run, pod, items, step=run.state["last_step"])
            self.clock.sleep(1)

    def _lost(self, run: Run, lost_since: float | None) -> float:
        lost_since = lost_since if lost_since is not None else self.clock.monotonic()
        if self.clock.monotonic() - lost_since >= LOST_FOR_GOOD:
            raise self._infra(
                run,
                "lost the connection to the cluster for good",
                [f"for {LOST_FOR_GOOD}s"],
                "the run ends by itself; " + self.cleanup_hint(run),
                step=run.state["last_step"],
                see="RFC-0005 §4.8",
            )
        return lost_since

    def _poll_ranks(self, run: Run) -> None:
        """Two pods: a log poll every POLL seconds instead of logs -f, which would keep
        blocking on a live pod after the other died. A resume repeats the lines of the
        since-second, so each pod skips what it already printed."""
        rank = {name: i for i, name in enumerate(run.state["pods"])}
        seen: dict[str, list] = {name: [None, 0] for name in rank}
        finished: set[str] = set()
        run.state["last_step"] = None
        lost_since = None
        while True:
            stamps = [v[0] for v in seen.values()]
            since = None if None in stamps else min(stamps)[:19] + "Z"
            try:
                items = self.kube.run_status(run.run_id).get("items", [])
            except LostConnection:
                lost_since = self._lost(run, lost_since)
                self.clock.sleep(POLL)
                continue
            # A dead rank's logs can be unreachable with its node, so its status decides
            # before a failing logs call reads as a lost connection.
            logs_error = None
            try:
                lines = self.kube.logs(selector=f"ostia.dev/run-id={run.run_id}", since_time=since)
            except (LostConnection, KubeError) as e:
                lines, logs_error = [], e
            skip = {name: v[1] for name, v in seen.items()}
            for raw in lines:
                tag, _, rest = raw.partition(" ")
                name = tag.removeprefix("[pod/").split("/")[0]
                if name not in seen:
                    continue
                ts, _, line = rest.partition(" ")
                last = seen[name][0]
                if last and ts < last:
                    continue
                if ts == last and skip[name]:
                    skip[name] -= 1
                    continue
                if ts != last:
                    seen[name] = [ts, 0]
                seen[name][1] += 1
                print(f"[rank {rank[name]}] {line}", flush=True)
                if line.startswith("[ostia] step ") and " (" in line:
                    run.state["last_step"] = line.split()[2]
                if line.startswith(FINISHED):
                    finished.add(name)
            if len(finished) == len(rank):
                return
            pods = self._pods(run, items)
            dead = [p for p in pods if p["status"].get("phase") not in ("Running", "Pending")]
            if dead:
                self._terminal(run, self._culprit(dead), items, step=run.state["last_step"])
            if len(pods) < len(rank):
                self._terminal(run, None, items, step=run.state["last_step"])
            if isinstance(logs_error, LostConnection):
                lost_since = self._lost(run, lost_since)
            elif logs_error is not None:
                raise logs_error
            else:
                lost_since = None
            self.clock.sleep(POLL)

    @staticmethod
    def _culprit(dead: list[dict]) -> dict:
        """The pod that failed first: once one rank fails, the Job controller deletes the
        other, so a concrete reason, then a pod not being deleted, names the cause."""

        def concrete(p: dict) -> bool:
            st = p.get("status", {})
            oom = any(
                cs.get("state", {}).get("terminated", {}).get("reason") == "OOMKilled"
                for cs in st.get("containerStatuses", [])
            )
            disrupted = any(c.get("type") == "DisruptionTarget" for c in st.get("conditions", []))
            return oom or disrupted or st.get("reason") == "Evicted"

        return (
            next((p for p in dead if concrete(p)), None)
            or next((p for p in dead if not p["metadata"].get("deletionTimestamp")), None)
            or dead[0]
        )

    def _terminal(self, run: Run, pod: dict | None, items: list[dict], *, step) -> None:
        profile = f"[remote.k8s.profiles.{run.profile.name}]"
        who = self._who(run, pod)
        job = next((i for i in items if i.get("kind") == "Job"), {})
        conds = {c.get("reason") for c in job.get("status", {}).get("conditions", [])}
        if "DeadlineExceeded" in conds:
            raise self._infra(
                run,
                f"the Job's deadline passed during step {step}",
                [],
                "raise --timeout, or shorten the run",
                step=step,
                see="RFC-0005 §4.8",
            )
        status = (pod or {}).get("status", {})
        for cs in status.get("containerStatuses", []):
            if cs.get("state", {}).get("terminated", {}).get("reason") == "OOMKilled":
                run.oom = True
                raise self._infra(
                    run,
                    f"{who}step {step} ran out of memory (OOMKilled)",
                    [f"memory limit: {run.profile.memory}"],
                    f"raise memory in {profile}, or lower the build parallelism",
                    step=step,
                    see="RFC-0005 §3.2",
                )
        if status.get("reason") == "Evicted":
            raise self._infra(
                run,
                f"{who}the pod was evicted",
                [status.get("message", "")],
                f"raise ephemeral_storage in {profile}",
                step=step,
            )
        for c in status.get("conditions", []):
            if c.get("type") == "DisruptionTarget":
                raise self._infra(
                    run,
                    f"{who}the node was preempted or went away ({c.get('reason')})",
                    [c.get("message", "")],
                    "rerun the same command",
                    step=step,
                )
        raise self._infra(
            run,
            f"{who}the pod ended before the run finished",
            [f"phase: {status.get('phase', 'gone')}"],
            "rerun; if it repeats, look at kubectl get events",
            step=step,
        )

    def _exec_tar(self, run: Run, pod: str, script: str, path: Path) -> None:
        with path.open("wb") as f:
            r = self.kube.exec_out(pod, ["sh", "-c", script], stdout=f)
        if r.returncode != 0:
            raise self._infra(
                run,
                "could not copy the results out of the pod",
                [f"exit {r.returncode}"],
                "rerun with -v",
                step="collect",
                see="RFC-0005 §3.4",
            )

    def _read_capture(self, pod: str, workdir: Path, rank: int):
        """capture.fetch's transport: three execs, so a missing manifest, an oversized
        directory and a failed tar each have their own answer before anything is copied."""
        calls = itertools.count()

        def run_sh(script: str, names: list[str], stdout=None):
            try:
                return self.kube.exec_out(pod, ["sh", "-c", script, "sh", *names], stdout=stdout)
            except OstiaError as e:  # kubectl itself failed: the pod or the connection is gone
                raise capture.CaptureTransportError("kubectl exec failed") from e

        def read_tar(names: list[str], limit: int) -> Path:
            r = run_sh(capture.probe_script(), [])
            if r.returncode == 3:
                raise capture.CaptureAbsent()
            if r.returncode != 0:
                raise capture.CaptureTransportError(f"the manifest probe exited {r.returncode}")
            r = run_sh(capture.sizes_script(), names)
            if r.returncode == 4:
                raise capture.CaptureNotRegular("a requested name is not a regular file")
            if r.returncode != 0:
                raise capture.CaptureTransportError(f"wc -c exited {r.returncode}")
            if capture.parse_sizes((r.stdout or b"").decode(errors="replace"), names) > limit:
                raise capture.CaptureTooLarge("the files are larger than the limit")
            path = workdir / f"capture{rank}-{next(calls)}.tar"
            with path.open("wb") as fh:
                r = run_sh(capture.tar_script(), names, stdout=fh)
            if r.returncode != 0:
                raise capture.CaptureTransportError(f"tar exited {r.returncode}")
            return path

        return read_tar

    def _fetch_captures(self, run: Run, workdir: Path) -> None:
        """Before the collected marker, so the pod still exists (RFC-0003 §4). A run with no
        capture anywhere leaves no trace; one with any gets capture/status.json."""
        root = run.results_dir / "capture"
        two = run.state["n"] > 1
        found = {}
        for i, pod in enumerate(run.state["pods"]):
            node = capture.NODE_DIRS[i]
            dest = root / node if two else root
            outcome = capture.fetch(self._read_capture(pod, workdir, i), dest)
            found[node] = capture.status_entry(outcome)
        if all(e["result"] == "absent" for e in found.values()):
            if root.is_dir() and not any(root.iterdir()):
                root.rmdir()  # fetch made it as the parent of node-<i>/
            return
        root.mkdir(parents=True, exist_ok=True)
        (root / "status.json").write_text(json.dumps(found, indent=2, sort_keys=True) + "\n")
        run.state["capture"] = found
        for node, entry in found.items():
            self._say(capture.describe(node, entry))

    def collect(self, run: Run, workdir: Path) -> list[Collected]:
        build_rel = run.plan.build_dir.removeprefix(suites.WORK + "/")
        out, failed = [], False
        for i, pod in enumerate(run.state["pods"]):
            control, artifacts = workdir / f"control{i}.tar", workdir / f"artifacts{i}.tar"
            self._exec_tar(run, pod, results.control_tar_script(), control)
            self._exec_tar(run, pod, results.artifact_tar_script(build_rel), artifacts)
            failed = failed or self._failed(control)
            out.append(Collected(f"rank-{i}" if run.state["n"] > 1 else "", control, artifacts))
        self._fetch_captures(run, workdir)
        if run.state["keep"] and failed:
            run.state["kept"] = True
            end = self.clock.now() + datetime.timedelta(seconds=run.state["keep"])
            execs = " | ".join(
                f"kubectl --context {self.target.context} --namespace "
                f"{self.target.namespace} exec -it {pod} -- sh"
                for pod in run.state["pods"]
            )
            self._say(
                f"kept for debugging until {end:%H:%M:%S} UTC: {execs}   (touch "
                f"/w/.ostia/collected, Ctrl-C or {self.cleanup_hint(run)} ends it)"
            )
        else:
            for i, pod in enumerate(run.state["pods"]):
                try:
                    self.kube.exec_out(pod, ["touch", f"{suites.WORK}/.ostia/collected"])
                except OstiaError:
                    # Everything is copied out already: without the marker the pod only waits
                    # out its collect window, and teardown deletes it either way.
                    who = f"rank {i}: " if run.state["n"] > 1 else ""
                    self._say(f"{who}could not mark the pod collected; it ends with its window")
        return out

    @staticmethod
    def _failed(control: Path) -> bool:
        try:
            with tarfile.open(control) as t:
                member = next(m for m in t.getmembers() if m.name.endswith("steps.json"))
                steps = json.load(io.BytesIO(t.extractfile(member).read()))
        except (StopIteration, tarfile.TarError, ValueError):
            return True
        return steps.get("state") != "done" or any(
            s["code"] != 0 and s["kind"] != "report" for s in steps.get("steps", [])
        )

    def _wait_kept(self, run: Run) -> None:
        end = self.clock.monotonic() + run.state["keep"]
        try:
            while self.clock.monotonic() < end:
                try:
                    pods = self._pods(run, self.kube.run_status(run.run_id).get("items", []))
                except LostConnection:
                    self.clock.sleep(POLL)
                    continue
                if not any(p["status"].get("phase") in ("Running", "Pending") for p in pods):
                    return
                self.clock.sleep(POLL)
        except KeyboardInterrupt:
            run.state["interrupted"] = True

    def _delete(self, run: Run) -> None:
        """RFC §4.8: a second Ctrl-C skips the wait, not the delete; kubectl inherits SIG_IGN."""
        previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            self.kube.delete("job", run.state["job"])
        finally:
            signal.signal(signal.SIGINT, previous)

    def teardown(self, run: Run) -> bool:
        if not run.state.get("created"):
            return True
        try:
            if run.state.get("kept"):
                self._wait_kept(run)
            self._delete(run)
            deadline = self.clock.monotonic() + TEARDOWN_WAIT
            while True:
                if not self.kube.run_status(run.run_id).get("items"):
                    return True
                if self.clock.monotonic() >= deadline:
                    return False
                self.clock.sleep(1)
        except (LostConnection, InfraError, UsageError):
            return False

    def describe(self, run: Run) -> dict:
        price = self.cfg.context(self.target.context).get("prices", {}).get(run.profile.name)
        host = {"host_network": True} if run.profile.host_network else {}
        return {
            **host,
            "context": self.target.context,
            "namespace": self.target.namespace,
            "provider": self.target.provider,
            "pod": run.state.get("pod"),
            "allow_unguarded": self.target.allow_unguarded,
            "notes": self.notes,
            "price": price,
            "cost_estimate": None,
            "kept": bool(run.state.get("kept")),
        }

    def cleanup_hint(self, run: Run) -> str:
        return (
            f"ostia-dev remote k8s cleanup --context {self.target.context} --namespace "
            f"{self.target.namespace} --run-id {run.run_id}"
        )
