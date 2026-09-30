"""Helpers for the kind tests: the real CLI and kubectl against the CI kind cluster.

Not a conftest.py: a second top-level `conftest` module would shadow tests/conftest.py.
"""

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


def run_args(*extra: str) -> list[str]:
    return ["--context", CONTEXT, "--profile", "cpu", *extra]


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
