"""The isolation on kind: default-deny, the private-range block, DNS, HTTPS (RFC-0005 §4.6)."""

import textwrap

import pytest
from kindlib import CONTEXT, NAMESPACE, cli, kubectl, logs_of, probe_job, wait_for

pytestmark = pytest.mark.kind
TARGET_NS = "ostia-kind-target"


@pytest.fixture(scope="module")
def server():
    kubectl("create", "namespace", TARGET_NS, namespace=None, check=False)
    script = "pixi exec --spec python python -m http.server 8443"
    probe_job("target-server", TARGET_NS, script, restricted=False)
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
    probe_job("control", TARGET_NS, PROBE.format(ip=server), restricted=False)
    assert "REACHED" in logs_of("control", TARGET_NS)


def test_default_deny_blocks_other_namespaces(server):
    once = PROBE.format(ip=server).replace("seq 1 60", "seq 1 3")
    probe_job("denied", NAMESPACE, once, restricted=True)
    assert "BLOCKED" in logs_of("denied", NAMESPACE)
    kubectl("delete", "job", "denied", "--wait=false", check=False)


def test_verify_passes_on_kind():
    r = cli("verify", "--context", CONTEXT, "--namespace", NAMESPACE, timeout=900)
    assert r.returncode == 0, r.stdout + r.stderr
    for check in ("metadata", "token", "apiserver", "node", "dns", "https"):
        assert f"PASS {check}" in r.stdout
