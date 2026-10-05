"""The fake `Kube` and clock the k8s tests run against (ostia-dev PR A2 task 2.3)."""

import io
import json
import time
from pathlib import Path

import pytest
from fakes.clock import FakeClock
from fakes.common import tar_bytes
from fakes.kube import (
    FakeKube,
    drop_stream,
    event,
    exec_result,
    log,
    lose_pod_on,
    plant_capture,
    pod_phase,
)

FIXTURES = Path(__file__).parent / "fixtures" / "kube"


def _job(name="ostia-r1", suspend=True, run_id="r1"):
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name, "labels": {"ostia.dev/run-id": run_id}},
        "spec": {
            "suspend": suspend,
            "template": {
                "metadata": {"labels": {"ostia.dev/run-id": run_id}},
                "spec": {
                    "containers": [
                        {
                            "name": "supervisor",
                            "env": [
                                {"name": "OSTIA_PLAN", "value": "install\tinstall\ttrue"},
                                {"name": "OSTIA_BUILD_DIR", "value": "/w/build/default/dev"},
                            ],
                        }
                    ]
                },  # fmt: skip
            },
        },
    }


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def kube(clock, tmp_path):
    return FakeKube(clock, root=tmp_path / "pods")


def test_clock_advances_only_when_told(clock):
    t0 = clock.now()
    assert clock.monotonic() == 0
    clock.sleep(90)
    assert clock.monotonic() == 90
    assert (clock.now() - t0).total_seconds() == 90
    assert t0.isoformat() == "2026-10-02T14:15:01+00:00"


def test_apply_get_delete_round_trip(kube, clock):
    created = kube.apply(_job())
    uid = created["metadata"]["uid"]
    assert len(uid) == 36
    got = kube.get("job", "ostia-r1")
    assert got["metadata"]["uid"] == uid
    assert got["metadata"]["creationTimestamp"] == "2026-10-02T14:15:01Z"
    kube.delete("job", "ostia-r1")
    assert kube.get("job", "ostia-r1") is None


def test_uid_is_stable_across_apply(kube):
    a = kube.apply(_job())["metadata"]["uid"]
    b = kube.apply(_job())["metadata"]["uid"]
    assert a == b


def test_create_refuses_an_existing_object(kube):
    kube.create(_job())
    with pytest.raises(FileExistsError):
        kube.create(_job())


def test_owner_cascade(kube):
    job = kube.apply(_job())
    kube.apply({
        "apiVersion": "v1", "kind": "Secret",
        "metadata": {"name": "ostia-r1-env", "ownerReferences": [
            {"kind": "Job", "name": "ostia-r1", "uid": job["metadata"]["uid"]}]},
    })  # fmt: skip
    kube.patch("job", "ostia-r1", {"spec": {"suspend": False}})
    assert kube.list("pod", selector="ostia.dev/run-id=r1")
    kube.delete("job", "ostia-r1")
    assert kube.get("secret", "ostia-r1-env") is None
    assert kube.list("pod", selector="ostia.dev/run-id=r1") == []


def test_unsuspend_creates_the_pod(kube):
    kube.apply(_job())
    assert kube.list("pod") == []
    kube.patch("job", "ostia-r1", {"spec": {"suspend": False}})
    (pod,) = kube.list("pod", selector="ostia.dev/run-id=r1")
    assert pod["metadata"]["name"].startswith("ostia-r1-")
    assert pod["metadata"]["labels"]["job-name"] == "ostia-r1"
    assert pod["status"]["phase"] == "Pending"
    assert "nodeName" not in pod["spec"]
    assert pod["spec"]["containers"][0]["env"][0]["name"] == "OSTIA_PLAN"


def test_scripted_timeline_fires_on_sleep(kube, clock):
    kube.apply(_job(suspend=False))
    kube.script([(5, pod_phase("Running")), (1200, event("FailedScheduling", "0/3 nodes"))])
    t0 = time.monotonic()
    clock.sleep(4)
    (pod,) = kube.list("pod")
    assert pod["status"]["phase"] == "Pending"
    clock.sleep(1)
    (pod,) = kube.list("pod")
    assert pod["status"]["phase"] == "Running"
    assert pod["spec"]["nodeName"] == "node-1"
    clock.sleep(1195)
    (ev,) = kube.events()
    assert ev["reason"] == "FailedScheduling"
    assert ev["involvedObject"]["uid"] == pod["metadata"]["uid"]
    assert time.monotonic() - t0 < 0.5


def test_terminal_pod_reasons(kube, clock):
    kube.apply(_job(suspend=False))
    kube.script([(1, pod_phase("Failed", reason="OOMKilled"))])
    clock.sleep(1)
    (pod,) = kube.list("pod")
    state = pod["status"]["containerStatuses"][0]["state"]["terminated"]
    assert (pod["status"]["phase"], state["reason"]) == ("Failed", "OOMKilled")


def test_run_status_is_the_job_and_its_pods(kube):
    kube.apply(_job(suspend=False))
    kube.apply(_job(name="ostia-other", run_id="other"))
    items = kube.run_status("r1")["items"]
    assert sorted(i["kind"] for i in items) == ["Job", "Pod"]


def test_calls_are_recorded_in_order(kube):
    kube.apply(_job())
    kube.patch("job", "ostia-r1", {"spec": {"suspend": False}})
    (pod,) = kube.list("pod")
    kube.exec_in(pod["metadata"]["name"], ["tar", "-x", "-C", "/w"], io.BytesIO(tar_bytes({})))
    kube.delete("job", "ostia-r1")
    assert [c.verb for c in kube.calls] == ["apply", "patch", "list", "exec_in", "delete"]
    assert kube.calls[3].stdin_size > 0


def test_gone_after_delays_deletion(kube, clock):
    kube.gone_after = 30
    kube.apply(_job())
    kube.delete("job", "ostia-r1")
    assert kube.get("job", "ostia-r1") is not None
    clock.sleep(30)
    assert kube.get("job", "ostia-r1") is None


def test_never_gone(kube, clock):
    kube.gone_after = None
    kube.apply(_job())
    kube.delete("job", "ostia-r1")
    clock.sleep(3600)
    assert kube.get("job", "ostia-r1") is not None


def test_exec_in_extracts_into_the_pod_dir(kube):
    kube.apply(_job(suspend=False))
    (pod,) = kube.list("pod")
    name = pod["metadata"]["name"]
    data = tar_bytes({"a.txt": b"a", "src/b.cpp": b"b"})
    assert kube.exec_in(name, ["tar", "-x", "-C", "/w"], io.BytesIO(data)).returncode == 0
    find = "find /w -type f ! -path '/w/.ostia/*' ! -path '/w/home/*' ! -path '/w/cache/*' | wc -l"
    assert kube.exec_out(name, ["sh", "-c", find]).stdout.strip() == b"2"
    kube.exec_in(name, ["touch", "/w/.ostia/ready"], None)
    assert kube.pod_file(name, "/w/.ostia/ready").exists()


def test_exec_result_overrides(kube):
    kube.apply(_job(suspend=False))
    (pod,) = kube.list("pod")
    kube.script([(0, exec_result(["tar", "-x"], 2))])
    r = kube.exec_in(pod["metadata"]["name"], ["tar", "-x", "-C", "/w"], io.BytesIO(b"x"))
    assert r.returncode == 2


def test_exec_out_gives_the_supervisors_control_files(kube):
    kube.apply(_job(suspend=False))
    (pod,) = kube.list("pod")
    kube.step_codes = {"install": 0}
    out = io.BytesIO()
    kube.exec_out(pod["metadata"]["name"], ["sh", "-c", "cd /w/.ostia && tar -cf - …"], stdout=out)
    import tarfile

    with tarfile.open(fileobj=io.BytesIO(out.getvalue())) as t:
        steps = json.loads(t.extractfile("steps.json").read())
    assert [s["name"] for s in steps["steps"]] == ["install"]


def test_logs_follow_yields_timestamped_lines_and_drops(kube, clock):
    kube.apply(_job(suspend=False))
    kube.script([(1, pod_phase("Running")), (2, log(["a", "b"])), (2, drop_stream()),
                 (3, log(["c"])), (4, pod_phase("Succeeded"))])  # fmt: skip
    clock.sleep(1)
    (pod,) = kube.list("pod")
    name = pod["metadata"]["name"]
    first = list(kube.logs_follow(name))
    assert [line.split(" ", 1)[1] for line in first] == ["a", "b"]
    ts = first[-1].split(" ", 1)[0]
    assert ts.endswith("Z") and "T" in ts
    second = list(kube.logs_follow(name, since_time=ts))
    assert [line.split(" ", 1)[1] for line in second] == ["a", "b", "c"]  # same timestamp again


def test_fixture_shapes_load():
    for f in sorted(FIXTURES.glob("*.json")):
        assert "kind" in json.loads(f.read_text()), f.name


INDEX = "batch.kubernetes.io/job-completion-index"


def _indexed_job(name="ostia-r2", run_id="r2"):
    job = _job(name=name, suspend=False, run_id=run_id)
    job["spec"].update(completionMode="Indexed", completions=2, parallelism=2)
    job["spec"]["template"]["spec"]["subdomain"] = name
    env = job["spec"]["template"]["spec"]["containers"][0]["env"]
    env.append(
        {
            "name": "OSTIA_RANK",
            "valueFrom": {"fieldRef": {"fieldPath": f"metadata.annotations['{INDEX}']"}},
        }
    )
    env.append({"name": "NODE_NAME", "valueFrom": {"fieldRef": {"fieldPath": "spec.nodeName"}}})
    return job


@pytest.fixture
def two(tmp_path):
    clock = FakeClock()
    kube = FakeKube(clock, root=tmp_path / "pods")
    kube.apply(_indexed_job())
    pods = sorted(kube.list("pod"), key=lambda p: p["metadata"]["annotations"][INDEX])
    return kube, clock, [p["metadata"]["name"] for p in pods]


def test_indexed_job_creates_two_named_pods(two):
    kube, _, names = two
    pods = {p["metadata"]["name"]: p for p in kube.list("pod")}
    for i, name in enumerate(names):
        assert name.startswith(f"ostia-r2-{i}-")
        meta, spec = pods[name]["metadata"], pods[name]["spec"]
        assert meta["annotations"][INDEX] == str(i) and meta["labels"][INDEX] == str(i)
        assert spec["hostname"] == f"ostia-r2-{i}" and spec["subdomain"] == "ostia-r2"


def test_plain_job_keeps_one_pod(tmp_path):
    kube = FakeKube(FakeClock(), root=tmp_path / "pods")
    kube.apply(_job(suspend=False))
    [pod] = kube.list("pod")
    assert INDEX not in pod["metadata"].get("annotations", {})


def test_pod_phase_by_index(two):
    kube, clock, names = two
    kube.script([(1, pod_phase("Running", index=1)), (2, pod_phase("Running"))])
    clock.sleep(1)
    phases = {p["metadata"]["name"]: p["status"]["phase"] for p in kube.list("pod")}
    assert phases == {names[0]: "Pending", names[1]: "Running"}
    clock.sleep(1)
    nodes = {p["metadata"]["name"]: p["spec"]["nodeName"] for p in kube.list("pod")}
    assert nodes == {names[0]: "node-1", names[1]: "node-2"}


def test_per_pod_logs_and_codes(two):
    kube, clock, names = two
    kube.rank_step_codes = {1: {"install": 7}}
    kube.script([(1, log(["zero"], index=0)), (1, log(["one"], index=1))])
    clock.sleep(1)
    for i, name in enumerate(names):
        buf = io.BytesIO()
        kube.exec_out(name, ["sh", "-c", "cd /w/.ostia && tar -cf - ."], stdout=buf)
        import tarfile

        with tarfile.open(fileobj=io.BytesIO(buf.getvalue())) as t:
            steps = json.loads(t.extractfile("steps.json").read())
            log_text = t.extractfile("log.txt").read().decode()
        assert steps["steps"][0]["code"] == (7 if i else 0)
        assert log_text == ("one\n" if i else "zero\n")


def test_logs_follow_selector_interleaves_with_prefix(two):
    kube, clock, names = two
    kube.script(
        [
            (0, pod_phase("Running")),
            (1, log(["b0"], index=1)),
            (1, log(["a0"], index=0)),
            (2, log(["a1"], index=0)),
            (3, pod_phase("Succeeded")),
        ]
    )
    clock.sleep(2)
    lines = list(kube.logs_follow(selector="ostia.dev/run-id=r2", prefix=True))
    bodies = [(line.split(" ")[0], line.split(" ", 2)[2]) for line in lines]
    assert bodies == [
        (f"[pod/{names[0]}/supervisor]", "a0"),
        (f"[pod/{names[1]}/supervisor]", "b0"),
        (f"[pod/{names[0]}/supervisor]", "a1"),
    ]


def test_field_ref_env_resolves_rank(two):
    kube, _, names = two
    assert [kube._env(n, "OSTIA_RANK") for n in names] == ["0", "1"]


def test_field_ref_env_resolves_the_node_name(two):
    kube, clock, names = two
    assert [kube._env(n, "NODE_NAME") for n in names] == ["fake-node-0", "fake-node-1"]
    kube.script([(0, pod_phase("Running"))])
    assert [kube._env(n, "NODE_NAME") for n in names] == ["node-1", "node-2"]


def test_a_once_result_answers_only_the_first_exec(kube):
    kube.apply(_job(suspend=False))
    (pod,) = kube.list("pod")
    name = pod["metadata"]["name"]
    kube.script([(0, exec_result(["tar", "-x"], 2, once=True))])
    assert kube.exec_in(name, ["tar", "-x", "-C", "/w"], io.BytesIO(tar_bytes({}))).returncode == 2
    assert kube.exec_in(name, ["tar", "-x", "-C", "/w"], io.BytesIO(tar_bytes({}))).returncode == 0


def test_capture_execs_read_the_pods_capture_directory(kube):
    import tarfile

    from ostia_dev.remote import capture

    kube.apply(_job(suspend=False))
    (pod,) = kube.list("pod")
    name = pod["metadata"]["name"]

    def sh(script, *names, stdout=None):
        return kube.exec_out(name, ["sh", "-c", script, "sh", *names], stdout=stdout)

    assert sh(capture.probe_script()).returncode == 3
    kube.script([(0, plant_capture({"manifest.json": b"{}", "hwloc.xml": b"<x/>"}))])
    assert sh(capture.probe_script()).returncode == 0
    sizes = sh(capture.sizes_script(), "manifest.json", "hwloc.xml").stdout.decode()
    assert capture.parse_sizes(sizes, ["manifest.json", "hwloc.xml"]) == 6
    assert sh(capture.sizes_script(), "nvml.json").returncode == 4
    buf = io.BytesIO()
    assert sh(capture.tar_script(), "hwloc.xml", stdout=buf).returncode == 0
    with tarfile.open(fileobj=io.BytesIO(buf.getvalue())) as t:
        assert t.extractfile("hwloc.xml").read() == b"<x/>"
    assert sh(capture.tar_script(), "nvml.json", stdout=io.BytesIO()).returncode != 0


def test_a_lost_pod_fails_every_later_exec_in_kubectl(kube):
    from ostia_dev.errors import InfraError

    kube.apply(_job(suspend=False))
    (pod,) = kube.list("pod")
    name = pod["metadata"]["name"]
    kube.script([(0, lose_pod_on("tar -cf - --"))])
    assert kube.exec_out(name, ["touch", "/w/.ostia/ready"]).returncode == 0
    with pytest.raises(InfraError):
        kube.exec_out(name, ["sh", "-c", 'cd /w/capture && tar -cf - -- "$@"', "sh", "a"])
    with pytest.raises(InfraError):
        kube.exec_out(name, ["touch", "/w/.ostia/collected"])
