"""Helpers for the kind tests: the real CLI and kubectl against the CI kind cluster.

Not a conftest.py: a second top-level `conftest` module would shadow tests/conftest.py.
"""

import datetime
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from ostia_dev.remote.k8s import kubectl as pinned

CONTEXT = "kind-ostia"
NAMESPACE = "ostia-test"
CONFIG = Path(__file__).parent / "config.toml"
EXE = shutil.which("ostia-dev")
os.environ.setdefault("OSTIA_CONFIG", str(CONFIG))


def cli_argv(*args: str) -> list[str]:
    return [EXE, "remote", "k8s", *args]


def cli(*args: str, timeout: float = 1500) -> subprocess.CompletedProcess:
    return subprocess.run(
        cli_argv(*args),
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, "OSTIA_CONFIG": str(CONFIG)},
    )


def run_args(*extra: str, profile: str = "cpu") -> list[str]:
    return ["--context", CONTEXT, "--profile", profile, *extra]


def kubectl(*args: str, namespace: str | None = NAMESPACE, check: bool = True, **kw):
    ns = ["--namespace", namespace] if namespace else []
    return subprocess.run(
        [str(pinned.ensure()), "--context", CONTEXT, *ns, *args],
        capture_output=True,
        text=True,
        check=check,
        **kw,
    )


def objects(run_selector: str = "ostia.dev/managed=true") -> list[dict]:
    out = kubectl("get", "jobs,pods", "-l", run_selector, "-o", "json").stdout
    return json.loads(out)["items"]


def wait_for(predicate, timeout: float, what: str, every: float = 2):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if value := predicate():
            return value
        time.sleep(every)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


def probe_job(
    name: str, namespace: str, script: str, *, restricted: bool, run_id: str | None = None
) -> None:
    """A Job with the run's pod spec (from `ostia-dev`'s own manifests) running `script`."""
    from ostia_dev import config
    from ostia_dev.remote.k8s import admin, manifests

    cfg = config.load()
    run = admin._probe_run(cfg, "cpu", "generic")
    job = manifests.job(
        run,
        namespace=namespace,
        script=script,
        owner="kindtest",
        now=datetime.datetime.now(datetime.UTC),
    )
    job["metadata"]["name"] = name
    job["spec"]["suspend"] = False
    # The kind node has 4 CPUs; the long-running server and a probe must fit beside a
    # 2-CPU run, and resources don't change what the policies allow.
    small = {"cpu": "250m", "memory": "512Mi", "ephemeral-storage": "2Gi"}
    job["spec"]["template"]["spec"]["containers"][0]["resources"] = {
        "requests": dict(small),
        "limits": dict(small),
    }
    if run_id:  # the label the per-run policy selects on
        job["spec"]["template"]["metadata"]["labels"]["ostia.dev/run-id"] = run_id
    if not restricted:
        job["spec"]["template"]["spec"]["serviceAccountName"] = "default"
    kubectl("apply", "-f", "-", input=json.dumps(job), namespace=namespace)


def logs_of(name: str, namespace: str) -> str:
    def done():
        out = kubectl("get", "job", name, "-o", "json", namespace=namespace, check=False)
        status = json.loads(out.stdout or "{}").get("status", {})
        return status.get("succeeded") or status.get("failed")

    wait_for(done, 600, f"job {name}")
    return kubectl("logs", f"job/{name}", namespace=namespace).stdout
