"""Two-pod runs on kind (RFC-0005 §4.11, Testing: kind rows; A3 Task 11).

kind has one node, so these runs use --same-node; anti-affinity itself is a golden test.
"""

import datetime
import json
import re
import textwrap
from pathlib import Path

import pytest
from kindlib import NAMESPACE, cli, kubectl, logs_of, objects, probe_job, run_args, wait_for

pytestmark = pytest.mark.kind

# One line: the supervisor's step plan can't carry newlines, so exec() gets escaped ones.
RENDEZVOUS = (
    "exec('import os, socket, time\\n"
    'rank, port, peer = os.environ["OSTIA_RANK"], int(os.environ["OSTIA_PORT"]), '
    'os.environ["OSTIA_PEER_HOST"]\\n'
    'if rank == "0":\\n'
    '    c, _ = socket.create_server(("", port)).accept()\\n'
    "    c.sendall(c.recv(4))\\n"
    "else:\\n"
    "    end = time.monotonic() + 120\\n"
    "    while True:\\n"
    "        try:\\n"
    "            c = socket.create_connection((peer, port), timeout=5)\\n"
    "            break\\n"
    "        except OSError:\\n"
    "            if time.monotonic() > end: raise\\n"
    "            time.sleep(2)\\n"
    '    c.sendall(b"ping")\\n'
    '    assert c.recv(4) == b"ping"\\n'
    'print("ok rank", rank)\')'
)


def _results(stdout: str) -> Path:
    line = next(line for line in stdout.splitlines() if line.startswith("results: "))
    return Path(line.removeprefix("results: "))


def test_two_pod_rendezvous():
    args = run_args(
        "--pods", "2", "--same-node", "--no-build", "--", "python", "-c", RENDEZVOUS,
        profile="cpu-small",
    )  # fmt: skip
    r = cli(*args)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-4000:]
    results = _results(r.stdout)
    for rank in (0, 1):
        assert f"ok rank {rank}" in (results / f"rank-{rank}" / "log.txt").read_text()
    summary = json.loads((results / "summary.json").read_text())
    assert [x["rank"] for x in summary["ranks"]] == [0, 1]
    assert "[rank 1] ok rank 1" in r.stdout
    wait_for(lambda: objects(f"ostia.dev/run-id={summary['run_id']}") == [], 120, "teardown")


LOG_LINE = re.compile(r"^\[pod/[^/]+/supervisor\] \d{4}-\d\d-\d\dT[0-9:.]+Z .*$")


def test_logs_prefix_format():
    """The line format K8sBackend._poll_ranks parses, from a real API server."""
    from ostia_dev import config
    from ostia_dev.remote.k8s import admin, manifests

    run = admin._probe_run(config.load(), "cpu-small", "generic")
    job = manifests.job(
        run,
        namespace=NAMESPACE,
        script="echo hello from $OSTIA_RANK",
        owner="kindtest",
        now=datetime.datetime.now(datetime.UTC),
        pods=2,
        same_node=True,
    )
    job["spec"]["suspend"] = False
    kubectl("apply", "-f", "-", input=json.dumps(job))
    name = job["metadata"]["name"]

    def done():
        out = kubectl("get", "job", name, "-o", "json", check=False).stdout or "{}"
        return json.loads(out).get("status", {}).get("succeeded") == 2

    wait_for(done, 300, f"job {name}")
    out = kubectl(
        "logs", "-l", f"ostia.dev/run-id={run.run_id}", "--prefix", "--timestamps", "--tail=-1"
    ).stdout
    lines = out.splitlines()
    assert lines and all(LOG_LINE.match(line) for line in lines), out
    assert {"hello from 0", "hello from 1"} <= {line.split(" ", 2)[2] for line in lines}
    kubectl("delete", "job", name, "--wait=false", check=False)


SERVE = "pixi exec --spec python python -m http.server 8443"
CLIENT = textwrap.dedent("""\
    for i in $(seq 1 {tries}); do
      if pixi exec --spec curl curl -sS -o /dev/null --max-time 5 http://{ip}:8443/; then
        echo REACHED; exit 0; fi
      sleep 2
    done
    echo BLOCKED
""")


def test_intra_run_allow_cross_run_deny():
    from ostia_dev.remote.k8s import manifests

    policy = manifests.run_policy("runa", {}, "kindtest")
    del policy["metadata"]["ownerReferences"]  # an owner that doesn't exist would GC it
    kubectl("apply", "-f", "-", input=json.dumps(policy))
    try:
        probe_job("runa-server", NAMESPACE, SERVE, restricted=True, run_id="runa")
        ip = wait_for(
            lambda: kubectl(
                "get", "pods", "-l", "job-name=runa-server",
                "-o", "jsonpath={.items[0].status.podIP}", check=False,
            ).stdout.strip(),
            300,
            "the server pod IP",
        )  # fmt: skip
        probe_job("runa-client", NAMESPACE, CLIENT.format(ip=ip, tries=60), restricted=True,
                  run_id="runa")  # fmt: skip
        probe_job("runb-client", NAMESPACE, CLIENT.format(ip=ip, tries=3), restricted=True,
                  run_id="runb")  # fmt: skip
        assert "REACHED" in logs_of("runa-client", NAMESPACE)
        assert "BLOCKED" in logs_of("runb-client", NAMESPACE)
    finally:
        for job in ("runa-server", "runa-client", "runb-client"):
            kubectl("delete", "job", job, "--wait=false", check=False)
        kubectl("delete", "networkpolicy", policy["metadata"]["name"], check=False)
