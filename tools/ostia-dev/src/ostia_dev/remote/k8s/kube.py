"""The one seam to the cluster (RFC-0005 §4.1): kubectl as a subprocess, errors mapped.

Every call passes --context, and --namespace where namespaced; the tests replace this class
with tests/fakes/kube.py:FakeKube.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from ostia_dev import proc
from ostia_dev.contract import violation
from ostia_dev.errors import InfraError, UsageError

ROLE = "ostia-test-developer"
_FORBIDDEN = re.compile(r'cannot (\S+) resource "([^"]+)"')
_PLUGIN = re.compile(r"executable (\S+) not found")
_UNAUTHORIZED = ("(Unauthorized)", "token has expired", "must be logged in", "invalid_grant")
_CONNECTION = (
    "was refused",
    "connection refused",
    "Unable to connect to the server",
    "i/o timeout",
    "connection reset",
    "unexpected EOF",
    "no such host",
    "TLS handshake timeout",
    "http2: client connection lost",
)
_EXEC_FAILED = "command terminated with exit code"
_PLUGIN_FIX = {
    "gke-gcloud-auth-plugin": "gcloud components install gke-gcloud-auth-plugin",
    "aws": "install the AWS CLI, then aws eks update-kubeconfig --name <cluster>",
    "kubelogin": "az aks install-cli",
}
_LOGIN_FIX = {"gke": "gcloud auth login", "eks": "aws sso login", "aks": "az login"}


class KubeError(UsageError):
    pass


class LostConnection(InfraError):
    pass


def forbidden_error(verb: str, resource: str, namespace: str | None, context: str) -> KubeError:
    return KubeError(
        violation(
            f"kubectl may not {verb} {resource} in namespace {namespace or '(cluster)'}",
            [f"context: {context}"],
            f"remote runs need the Role {ROLE} (RFC-0005 §4.3)",
            f"ask a cluster admin to run ostia-dev remote k8s init --context {context} "
            f"--namespace {namespace} and bind the Role it prints to you",
            "RFC-0005 §4.3",
        )
    )


def map_error(stderr: str, *, provider: str | None, context: str, namespace: str | None):
    err = stderr.strip()
    if m := _FORBIDDEN.search(err):
        return forbidden_error(m.group(1), m.group(2), namespace, context)
    if m := _PLUGIN.search(err):
        plugin = m.group(1)
        return KubeError(
            violation(
                f"the credential plugin {plugin} is not installed",
                [f"context: {context}"],
                "kubectl uses the credential plugin your kubeconfig names",
                _PLUGIN_FIX.get(plugin, f"install {plugin}, the plugin your kubeconfig names"),
                "RFC-0005 §4.1",
            )
        )
    if any(s in err for s in _UNAUTHORIZED):
        return KubeError(
            violation(
                "the cluster refused your credentials: they are missing or expired",
                [f"context: {context}", err.splitlines()[-1]],
                "kubectl uses your kubeconfig's credentials unchanged",
                _LOGIN_FIX.get(provider or "", "log in to the cluster again"),
                "RFC-0005 §4.1",
            )
        )
    if any(s in err for s in _CONNECTION):
        return LostConnection(
            violation(
                "lost the connection to the cluster",
                [f"context: {context}", err.splitlines()[-1] if err else "(no error text)"],
                "a run whose connection is gone ends by itself (RFC-0005 §4.8)",
                "check the network or VPN, then rerun; ostia-dev remote k8s cleanup removes "
                "anything left",
                "RFC-0005 §4.8",
            )
        )
    return InfraError(
        violation(
            "kubectl failed",
            [f"context: {context}", *err.splitlines()[-5:]],
            "an unexpected kubectl error leaves the run's result unknown",
            "rerun with -v to see the kubectl commands",
            "RFC-0005 §4.1",
        )
    )


class Kube:
    def __init__(
        self,
        kubectl: Path,
        context: str,
        namespace: str | None,
        *,
        provider: str | None = None,
        runner=proc,
    ) -> None:
        self.kubectl = kubectl
        self.context = context
        self.namespace = namespace
        self.provider = provider
        self.runner = runner

    def _argv(self, args: list[str], namespaced: bool) -> list[str]:
        base = [str(self.kubectl), "--context", self.context]
        if namespaced and self.namespace:
            base += ["--namespace", self.namespace]
        return [*base, *args]

    def _error(self, stderr) -> Exception:
        text = stderr.decode(errors="replace") if isinstance(stderr, bytes) else stderr or ""
        return map_error(
            text, provider=self.provider, context=self.context, namespace=self.namespace
        )

    def _run(self, args: list[str], *, namespaced: bool = True, **kw):
        r = self.runner.run(self._argv(args, namespaced), **kw)
        if r.returncode != 0:
            raise self._error(r.stderr)
        return r

    def _json(self, args: list[str], *, namespaced: bool = True, **kw) -> dict:
        out = self._run(args, namespaced=namespaced, **kw).stdout
        return json.loads(out) if out and out.strip() else {}

    def version(self) -> dict:
        return self._json(["version", "-o", "json"], namespaced=False)

    def get(self, kind: str, name: str, *, namespaced: bool = True) -> dict | None:
        doc = self._json(
            ["get", kind, name, "-o", "json", "--ignore-not-found"], namespaced=namespaced
        )
        return doc or None

    def list(
        self,
        kind: str,
        *,
        selector: str | None = None,
        namespaced: bool = True,
        all_namespaces: bool = False,
    ) -> list:
        sel = ["-l", selector] if selector else []
        every = ["-A"] if all_namespaces else []
        args = ["get", kind, *sel, *every, "-o", "json"]
        return self._json(args, namespaced=namespaced and not all_namespaces).get("items", [])

    def apply(self, obj: dict) -> dict:
        return self._json(["apply", "-f", "-", "-o", "json"], input=json.dumps(obj))

    def create(self, obj: dict) -> dict:
        return self._json(["create", "-f", "-", "-o", "json"], input=json.dumps(obj))

    def patch(self, kind: str, name: str, patch: dict) -> dict:
        return self._json(
            ["patch", kind, name, "--type", "merge", "-p", json.dumps(patch), "-o", "json"]
        )

    def delete(
        self,
        kind: str,
        name: str | None = None,
        *,
        selector: str | None = None,
        wait: bool = False,
        namespaced: bool = True,
    ) -> None:
        target = [name] if name else ["-l", selector or ""]
        self._run(
            [
                "delete",
                kind,
                *target,
                "--ignore-not-found",
                f"--wait={'true' if wait else 'false'}",
                "--cascade=background",
            ],
            namespaced=namespaced,
        )

    def events(self) -> list[dict]:
        return self._json(["get", "events", "-o", "json"]).get("items", [])

    def run_status(self, run_id: str) -> dict:
        """The Job and its pods in one call (R13: at most two calls per status tick)."""
        return self._json(["get", "job,pods", "-l", f"ostia.dev/run-id={run_id}", "-o", "json"])

    def logs_follow(self, pod: str, *, since_time: str | None = None):
        since = [f"--since-time={since_time}"] if since_time else []
        p = self.runner.stream(
            self._argv(["logs", "-f", "--timestamps", *since, pod], True), merge_stderr=False
        )
        try:
            for line in p.stdout:
                yield line.rstrip("\n")
        finally:
            p.terminate()
            p.wait()
        if p.returncode:
            raise self._error(p.stderr.read() if p.stderr else "")

    def _exec(self, args: list[str], check: bool, **kw) -> subprocess.CompletedProcess:
        r = self.runner.run(self._argv(args, True), **kw)
        err = r.stderr.decode(errors="replace") if isinstance(r.stderr, bytes) else r.stderr or ""
        if r.returncode != 0 and check and _EXEC_FAILED not in err:
            raise self._error(err)
        return r

    def exec_in(self, pod: str, argv: list[str], stdin, *, check: bool = True):
        """Returns the remote command's code; raises only when kubectl itself failed."""
        return self._exec(["exec", "-i", pod, "--", *argv], check, stdin=stdin)

    def exec_out(self, pod: str, argv: list[str], stdout=None, *, check: bool = True):
        if stdout is not None:
            return self._exec(
                ["exec", pod, "--", *argv],
                check,
                capture=False,
                stdout=stdout,
                stderr=subprocess.PIPE,
            )
        return self._exec(["exec", pod, "--", *argv], check, binary=True)

    def can_i(self, verb: str, resource: str) -> bool:
        r = self.runner.run(self._argv(["auth", "can-i", verb, resource], True))
        return r.returncode == 0
