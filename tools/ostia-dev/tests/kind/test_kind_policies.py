"""The isolation on kind: default-deny, the private-range block, DNS, HTTPS (RFC-0005 §4.6)."""

import datetime
import json
import textwrap

import pytest
from kindlib import CONTEXT, NAMESPACE, cli, kubectl, wait_for

pytestmark = pytest.mark.kind
TARGET_NS = "ostia-kind-target"


def _probe_job(name: str, namespace: str, script: str, *, restricted: bool) -> None:
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
    if not restricted:
        job["spec"]["template"]["spec"]["serviceAccountName"] = "default"
    kubectl("apply", "-f", "-", input=json.dumps(job), namespace=namespace)


def _logs(name: str, namespace: str) -> str:
    def done():
        out = kubectl("get", "job", name, "-o", "json", namespace=namespace, check=False)
        status = json.loads(out.stdout or "{}").get("status", {})
        return status.get("succeeded") or status.get("failed")

    wait_for(done, 600, f"job {name}")
    return kubectl("logs", f"job/{name}", namespace=namespace).stdout


@pytest.fixture(scope="module")
def server():
    kubectl("create", "namespace", TARGET_NS, namespace=None, check=False)
    script = "pixi exec --spec python python -m http.server 8443"
    _probe_job("target-server", TARGET_NS, script, restricted=False)
    wait_for(
        lambda: kubectl(
            "get",
            "pods",
            "-l",
            "job-name=target-server",
            "-o",
            "jsonpath={.items[0].status.podIP}",
            namespace=TARGET_NS,
            check=False,
        ).stdout.strip(),
        300,
        "the server pod IP",
    )
    ip = kubectl(
        "get",
        "pods",
        "-l",
        "job-name=target-server",
        "-o",
        "jsonpath={.items[0].status.podIP}",
        namespace=TARGET_NS,
    ).stdout.strip()
    yield ip
    kubectl("delete", "namespace", TARGET_NS, "--wait=false", namespace=None, check=False)


PROBE = textwrap.dedent("""\
    for i in $(seq 1 60); do
      if pixi exec --spec curl curl -sS -o /dev/null --max-time 5 http://{ip}:8443/; then
        echo REACHED; exit 0; fi
      sleep 2
    done
    echo BLOCKED
""")


def test_the_server_is_reachable_without_a_policy(server):
    _probe_job("control", TARGET_NS, PROBE.format(ip=server), restricted=False)
    assert "REACHED" in _logs("control", TARGET_NS)


def test_default_deny_blocks_other_namespaces(server):
    once = PROBE.format(ip=server).replace("seq 1 60", "seq 1 3")
    _probe_job("denied", NAMESPACE, once, restricted=True)
    assert "BLOCKED" in _logs("denied", NAMESPACE)
    kubectl("delete", "job", "denied", "--wait=false", check=False)


def test_verify_passes_on_kind():
    r = cli("verify", "--context", CONTEXT, "--namespace", NAMESPACE, timeout=900)
    assert r.returncode == 0, r.stdout + r.stderr
    for check in ("metadata", "token", "apiserver", "node", "dns", "https"):
        assert f"PASS {check}" in r.stdout
