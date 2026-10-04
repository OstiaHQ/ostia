"""The single-pod k8s lifecycle against FakeKube (RFC-0005 §4.2, §4.5, §4.8; R4, R13).

Runtime rows of the RFC's Failure-handling table are `test_failure_<row>`.
"""

import json
from pathlib import Path

import pytest
from fakes.capture import capture_files
from fakes.clock import FakeClock
from fakes.kube import (
    FakeKube,
    drop_stream,
    event,
    exec_result,
    log,
    logs_fail,
    lose_connection,
    lose_pod_on,
    plant_capture,
    pod_phase,
)
from ostia_dev import config
from ostia_dev.remote import core
from ostia_dev.remote.k8s import manifests
from ostia_dev.remote.k8s.backend import FINISHED, K8sBackend
from ostia_dev.remote.k8s.preflight import Target

OK_LOG = [
    "[ostia] step install (install): pixi install --locked -e default",
    f"{FINISHED} 0; waiting for the results to be collected (at most 600s)",
]
HAPPY = [(3, pod_phase("Running")), (9, log(OK_LOG))]


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def fake(clock, tmp_path):
    k = FakeKube(clock, root=tmp_path / "pods")
    for obj in manifests.guardrails("ostia-test"):
        k.apply(obj, record=False)
    return k


@pytest.fixture
def repo(git_repo, monkeypatch):
    monkeypatch.setattr(core, "check_lock", lambda root: None)
    return git_repo


@pytest.fixture
def cfg(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("schema = 1\n[remote.k8s.contexts.c1.prices]\ncpu = 0.2\n")
    return config.load(path)


def _drive(
    fake,
    clock,
    cfg,
    repo,
    tmp_path,
    timeline=HAPPY,
    *,
    envs=("default",),
    profile="cpu",
    provider="gke",
    nodes=(),
    **extra,
):
    target = Target(
        context="c1",
        namespace="ostia-test",
        provider=provider,
        kubectl=Path("/k"),
        allow_unguarded=bool(extra.get("allow_unguarded")),
        nodes=list(nodes),
    )
    backend = K8sBackend(cfg=cfg, target=target, kube=fake, clock=clock)
    spec = core.RunSpec(
        backend="k8s",
        profile=profile,
        envs=list(envs),
        results=tmp_path / "res",
        provider="gke",
        env_vars=extra.pop("env_vars", []),
        allow_secret=extra.pop("allow_secret", False),
        command=extra.pop("command", None),
        extra={"context": "c1", **extra},
    )
    fake.script(timeline)
    return core.drive(backend, spec, cfg=cfg, repo=repo)


def _summary(tmp_path) -> dict:
    (run_dir,) = sorted((tmp_path / "res").iterdir())[-1:]
    return json.loads((run_dir / "summary.json").read_text())


def _verbs(fake) -> list[str]:
    return [c.verb for c in fake.calls]


def test_a_passing_run(fake, clock, cfg, repo, tmp_path, capsys):
    assert _drive(fake, clock, cfg, repo, tmp_path) == 0
    s = _summary(tmp_path)
    assert (s["exit_code"], s["teardown"], s["context"], s["namespace"]) == (
        0,
        "verified",
        "c1",
        "ostia-test",
    )
    assert s["node"] == "node-1" and s["price"] == 0.2 and s["cost_estimate"] is not None
    assert fake.list("job") == [] and fake.list("pod") == []
    out = capsys.readouterr().out
    assert "[ostia] step install" in out and s["run_id"] in out


def test_create_suspended_then_owned_then_unsuspend(fake, clock, cfg, repo, tmp_path):
    assert (
        _drive(
            fake,
            clock,
            cfg,
            repo,
            tmp_path,
            allow_secret=True,
            env_vars=["GH_TOKEN=s3cret", "MODE=fast"],
        )
        == 0
    )
    verbs = ("apply", "create", "patch", "delete")
    calls = [(c.verb, c.args[:2]) for c in fake.calls if c.verb in verbs]
    job = f"ostia-{_summary(tmp_path)['run_id']}"
    assert calls[:3] == [
        ("apply", ("Job", job)),
        ("create", ("Secret", f"{job}-env")),
        ("patch", ("job", job)),
    ]
    assert calls[-1] == ("delete", ("job", job))


def test_the_secret_is_owned_by_the_job_and_values_stay_out_of_the_job(
    fake, clock, cfg, repo, tmp_path
):
    seen = {}
    original = fake.apply

    def apply(obj, record=True):
        out = original(obj, record=record)
        seen[obj["kind"]] = out
        return out

    fake.apply = apply
    _drive(fake, clock, cfg, repo, tmp_path, allow_secret=True, env_vars=["GH_TOKEN=s3cret"])
    owner = seen["Secret"]["metadata"]["ownerReferences"][0]
    assert owner["uid"] == seen["Job"]["metadata"]["uid"]
    assert "s3cret" not in json.dumps(seen["Job"])


def test_status_poll_is_every_3s_with_at_most_2_calls(fake, clock, cfg, repo, tmp_path):
    timeline = [(60, pod_phase("Running")), (66, log(OK_LOG))]
    assert _drive(fake, clock, cfg, repo, tmp_path, timeline) == 0
    polls = [
        c for c in fake.calls[: _verbs(fake).index("exec_in")] if c.verb in ("run_status", "events")
    ]
    assert len(polls) <= 2 * (60 // 3 + 1)
    assert _verbs(fake).count("run_status") >= 60 // 3


def test_status_line_shows_the_scheduling_events(fake, clock, cfg, repo, tmp_path, capsys):
    timeline = [
        (3, event("FailedScheduling", "0/3 nodes are available: 3 Insufficient nvidia.com/gpu")),
        (6, event("TriggeredScaleUp", "pod triggered scale-up", type_="Normal")),
        (12, pod_phase("Running")),
        (15, log(OK_LOG)),
    ]
    assert _drive(fake, clock, cfg, repo, tmp_path, timeline) == 0
    err = capsys.readouterr().err
    assert "FailedScheduling" in err and "TriggeredScaleUp" in err


@pytest.mark.parametrize(
    ("slug", "message"),
    [
        ("quota", "exceeded quota: ostia-test-quota, requested: requests.nvidia.com/gpu=1"),
        ("psa", 'violates PodSecurity "restricted:latest": allowPrivilegeEscalation != false'),
    ],
)
def test_failure_quota_exceeded_psa_or_webhook_rejection(
    fake, clock, cfg, repo, tmp_path, slug, message, capsys
):
    timeline = [(3, event("FailedCreate", f"Error creating: {message}", involved="job"))]
    assert _drive(fake, clock, cfg, repo, tmp_path, timeline) == 2
    assert message in capsys.readouterr().err
    assert fake.list("job") == []
    assert clock.monotonic() < 60  # it stops at the first FailedCreate


def test_failure_pod_never_schedules(fake, clock, cfg, repo, tmp_path, capsys):
    timeline = [
        (30, event("FailedScheduling", "0/3 nodes are available: 3 node(s) had untolerated taint"))
    ]
    assert _drive(fake, clock, cfg, repo, tmp_path, timeline) == 3
    err = capsys.readouterr().err
    assert "untolerated taint" in err and "profiles.cpu" in err
    assert 1200 <= clock.monotonic() < 1300  # the 20-minute --schedule-timeout
    assert fake.list("job") == []


def test_schedule_timeout_flag(fake, clock, cfg, repo, tmp_path):
    assert _drive(fake, clock, cfg, repo, tmp_path, [], schedule_timeout="2m") == 3
    assert clock.monotonic() < 200


def test_failure_image_pull_fails(fake, clock, cfg, repo, tmp_path, capsys):
    timeline = [(3, event("Failed", 'Failed to pull image "x": not found; ErrImagePull'))]
    assert _drive(fake, clock, cfg, repo, tmp_path, timeline) == 3
    assert "ErrImagePull" in capsys.readouterr().err


def test_failure_cache_volume_in_another_zone_or_in_use(fake, clock, cfg, repo, tmp_path, capsys):
    timeline = [
        (
            3,
            event(
                "FailedScheduling",
                "0/3 nodes are available: 3 node(s) had volume node affinity conflict",
            ),
        )
    ]
    assert _drive(fake, clock, cfg, repo, tmp_path, timeline, cache=True) == 3
    err = capsys.readouterr().err
    assert "ostia-test-cache" in err and "cleanup --cache" in err
    assert fake.get("persistentvolumeclaim", "ostia-test-cache")  # the PVC is kept


def test_upload_then_count_then_ready(fake, clock, cfg, repo, tmp_path):
    assert _drive(fake, clock, cfg, repo, tmp_path) == 0
    execs = [c.args[1] for c in fake.calls if c.verb in ("exec_in", "exec_out")]
    assert execs[0][:2] == ("tar", "-x")
    assert execs[1][2].startswith("find /w -type f")
    assert execs[2] == ("touch", "/w/.ostia/ready")


@pytest.mark.parametrize("how", ["tar", "count"])
def test_failure_upload_fails(fake, clock, cfg, repo, tmp_path, how, capsys):
    prefix = ["tar", "-x"] if how == "tar" else ["sh", "-c"]
    out = b"" if how == "tar" else b"999\n"
    timeline = [(0, exec_result(prefix, 1 if how == "tar" else 0, out)), *HAPPY]
    assert _drive(fake, clock, cfg, repo, tmp_path, timeline) == 3
    assert not any(
        c.args[1] == ("touch", "/w/.ostia/ready") for c in fake.calls if c.verb == "exec_out"
    )
    assert _summary(tmp_path)["failing_step"] == "upload"
    assert "ephemeral_storage" in capsys.readouterr().err
    assert fake.list("job") == []


def test_logs_resume_without_duplicates(fake, clock, cfg, repo, tmp_path, capsys):
    timeline = [
        (3, pod_phase("Running")),
        (6, log(["A", "B", "C"])),
        (7, log(["D", "E"])),
        (7, drop_stream()),
        (8, log(["F", OK_LOG[-1]])),
    ]
    assert _drive(fake, clock, cfg, repo, tmp_path, timeline) == 0
    out = [line for line in capsys.readouterr().out.splitlines() if len(line) == 1]
    assert out == ["A", "B", "C", "D", "E", "F"]


def test_failure_log_stream_drops(fake, clock, cfg, repo, tmp_path):
    timeline = [(3, pod_phase("Running")), (6, log(["A"])), (6, drop_stream()), (8, log(OK_LOG))]
    assert _drive(fake, clock, cfg, repo, tmp_path, timeline) == 0
    assert _verbs(fake).count("logs_follow") == 2


def test_failure_connection_lost_for_good(fake, clock, cfg, repo, tmp_path, capsys):
    timeline = [(3, pod_phase("Running")), (6, log(["A"])), (6, lose_connection())]
    assert _drive(fake, clock, cfg, repo, tmp_path, timeline) == 3
    err = capsys.readouterr().err
    assert "cleanup --context c1 --namespace ostia-test --run-id" in err


@pytest.mark.parametrize(
    ("slug", "reason", "words"),
    [
        ("out_of_memory_kill", "OOMKilled", ["memory", "profiles.cpu"]),
        ("eviction_ephemeral_storage", "Evicted", ["ephemeral_storage"]),
        ("node_preempted", "PreemptionByScheduler", ["preempted", "rerun"]),
        ("deadline_reached", "DeadlineExceeded", ["deadline", "install"]),
    ],
)
def test_failure_terminal_pod_reasons(
    fake, clock, cfg, repo, tmp_path, slug, reason, words, capsys
):
    timeline = [
        (3, pod_phase("Running")),
        (6, log(OK_LOG[:1])),
        (9, pod_phase("Failed", reason=reason)),
    ]
    assert _drive(fake, clock, cfg, repo, tmp_path, timeline) == 3
    err = capsys.readouterr().err.lower()
    assert all(w.lower() in err for w in words), err
    assert fake.list("job") == []


def test_collect_then_collected(fake, clock, cfg, repo, tmp_path):
    assert _drive(fake, clock, cfg, repo, tmp_path) == 0
    outs = [c.args[1] for c in fake.calls if c.verb == "exec_out"]
    assert ".ostia && tar" in outs[-4][2] and "tar -cf" in outs[-3][2]
    assert outs[-2][2] == "test -f /w/capture/manifest.json || exit 3"
    assert outs[-1] == ("touch", "/w/.ostia/collected")
    run_dir = tmp_path / "res" / _summary(tmp_path)["run_id"]
    assert (run_dir / "junit.xml").exists() and (run_dir / "log.txt").exists()
    # no capture anywhere: no capture/ directory and no summary entry
    assert not (run_dir / "capture").exists() and _summary(tmp_path)["capture"] is None


CAPTURED = [(3, pod_phase("Running")), (5, plant_capture(capture_files())), (9, log(OK_LOG))]


def _capture_execs(fake) -> list[str]:
    return [
        c.args[1][2]
        for c in fake.calls
        if c.verb == "exec_out" and c.args[1][:2] == ("sh", "-c") and "/w/capture" in c.args[1][2]
    ]


def test_capture_is_fetched_manifest_first_before_collected(
    fake, clock, cfg, repo, tmp_path, capsys
):
    from ostia_dev.topo import manifest

    assert _drive(fake, clock, cfg, repo, tmp_path, CAPTURED) == 0
    execs = [c.args[1] for c in fake.calls if c.verb == "exec_out"]
    touch = execs.index(("touch", "/w/.ostia/collected"))
    capture_at = [i for i, a in enumerate(execs) if "/w/capture" in " ".join(a)]
    assert capture_at and max(capture_at) < touch
    first = [a for a in execs if "/w/capture" in " ".join(a)][:3]
    assert [a[2].split()[0] for a in first] == ["test", "cd", "cd"]
    assert first[1][4:] == ("manifest.json",) and "wc -c" in first[1][2]
    assert first[2][4:] == ("manifest.json",) and "tar -cf" in first[2][2]
    s = _summary(tmp_path)
    out = tmp_path / "res" / s["run_id"] / "capture"
    assert manifest.accept(out)["status"] == "complete"
    assert (
        json.loads((out / "status.json").read_text())
        == s["capture"]
        == {"node-0": {"result": "accepted", "reason": None, "status": "complete", "fix": None}}
    )
    io = capsys.readouterr()
    assert "[ostia] capture node-0: accepted (complete)" in io.err
    assert "  capture accepted (complete)  " in io.out


def test_two_pods_fetch_into_node_dirs_and_never_stamp(fake, clock, cfg, repo, tmp_path):
    timeline = [*TWO_OK, (5, plant_capture(capture_files()))]
    assert _drive(fake, clock, cfg, repo, tmp_path, timeline, pods=2) == 0
    s = _summary(tmp_path)
    out = tmp_path / "res" / s["run_id"] / "capture"
    assert (out / "node-0" / "manifest.json").exists()
    assert (out / "node-1" / "manifest.json").exists()
    assert set(s["capture"]) == {"node-0", "node-1"}
    assert not (out / "pair.json").exists()  # only remote gate writes the pair and stamps
    assert "topology_source" not in json.dumps(s)


def test_a_rejected_capture_keeps_only_diagnostics(fake, clock, cfg, repo, tmp_path, capsys):
    timeline = [*HAPPY, (5, plant_capture(capture_files(sanitized=False)))]
    assert _drive(fake, clock, cfg, repo, tmp_path, timeline) == 0
    s = _summary(tmp_path)
    out = tmp_path / "res" / s["run_id"] / "capture"
    assert sorted(p.name for p in out.iterdir()) == ["diagnostics.txt", "status.json"]
    assert s["capture"]["node-0"]["result"] == "rejected"
    assert "fixtures.md#leak-check" in capsys.readouterr().err


def test_a_symlink_in_the_capture_is_rejected(fake, clock, cfg, repo, tmp_path):
    files = capture_files()
    del files["nics.json"]
    timeline = [*HAPPY, (5, plant_capture(files, links={"nics.json": "hwloc.xml"}))]
    assert _drive(fake, clock, cfg, repo, tmp_path, timeline) == 0
    entry = _summary(tmp_path)["capture"]["node-0"]
    assert entry["result"] == "rejected" and "not a regular file" in entry["reason"]


def test_a_pod_lost_mid_fetch_rejects_the_capture_and_keeps_the_results(
    fake, clock, cfg, repo, tmp_path
):
    timeline = [*CAPTURED, (5, lose_pod_on("tar -cf - --"))]
    assert _drive(fake, clock, cfg, repo, tmp_path, timeline) == 0
    s = _summary(tmp_path)
    assert s["capture"]["node-0"]["result"] == "rejected"
    assert "rerun" in s["capture"]["node-0"]["fix"]
    run_dir = tmp_path / "res" / s["run_id"]
    assert (run_dir / "junit.xml").exists() and (run_dir / "log.txt").exists()
    assert s["teardown"] == "verified" and fake.list("job") == []


def test_one_failed_capture_exec_is_retried(fake, clock, cfg, repo, tmp_path):
    from ostia_dev.remote import capture

    flaky = exec_result(["sh", "-c", capture.tar_script()], 2, once=True)
    assert _drive(fake, clock, cfg, repo, tmp_path, [*CAPTURED, (5, flaky)]) == 0
    assert _summary(tmp_path)["capture"]["node-0"]["result"] == "accepted"
    tars = [e for e in _capture_execs(fake) if "tar -cf" in e]
    assert len(tars) == 4  # manifest twice, then the files and diagnostics.txt


def _job_env(fake, clock, cfg, repo, tmp_path, **kw) -> dict:
    seen = {}
    original = fake.apply

    def apply(obj, record=True):
        if obj.get("kind") == "Job":
            seen["job"] = obj
        return original(obj, record=record)

    fake.apply = apply
    assert _drive(fake, clock, cfg, repo, tmp_path, **kw) == 0
    env = seen["job"]["spec"]["template"]["spec"]["containers"][0]["env"]
    names = [e["name"] for e in env]
    assert len(names) == len(set(names))
    return {e["name"]: e.get("value", e.get("valueFrom")) for e in env}


def _node(name: str, instance_type: str | None) -> dict:
    labels = {"node.kubernetes.io/instance-type": instance_type} if instance_type else {}
    alloc = {"cpu": "8", "memory": "32Gi", "ephemeral-storage": "100Gi"}
    return {"metadata": {"name": name, "labels": labels}, "status": {"allocatable": alloc}}


@pytest.mark.parametrize(
    ("provider", "expected"),
    [("gke", "gcp"), ("eks", "aws"), ("aks", "azure"), ("generic", "unknown")],
)
def test_the_job_env_names_the_capture_provider(
    fake, clock, cfg, repo, tmp_path, provider, expected
):
    env = _job_env(fake, clock, cfg, repo, tmp_path, provider=provider)
    assert env["OSTIA_CAPTURE_PROVIDER"] == expected
    assert env["NODE_NAME"] == {"fieldRef": {"fieldPath": "spec.nodeName"}}


@pytest.mark.parametrize(
    ("types", "expected"),
    [(["g6.4xlarge", "g6.4xlarge"], "g6.4xlarge"), (["g6.4xlarge", "g6.2xlarge"], "unknown"),
     (["g6.4xlarge", None], "unknown"), ([], "unknown")],
)  # fmt: skip
def test_the_job_env_names_the_instance_type_only_when_unique(
    fake, clock, cfg, repo, tmp_path, types, expected
):
    nodes = [_node(f"n{i}", t) for i, t in enumerate(types)]
    env = _job_env(fake, clock, cfg, repo, tmp_path, nodes=nodes)
    assert env["OSTIA_CAPTURE_INSTANCE_TYPE"] == expected


@pytest.mark.parametrize(
    ("cluster", "expected"),
    [("arn:aws:eks:us-west-2:123456789012:cluster/ostia-gpu-prod",
      "123456789012\nostia-gpu-prod"),
     ("gke_ostia-gpu-project_us-central1-a_ostia-l4-pool", "ostia-gpu-project\nostia-l4-pool")],
)  # fmt: skip
def test_the_job_env_lists_the_leak_identifiers(
    fake, clock, cfg, repo, tmp_path, cluster, expected
):
    fake.cluster = cluster
    env = _job_env(fake, clock, cfg, repo, tmp_path)
    assert env["OSTIA_LEAK_IDENTIFIERS"] == expected


def test_an_env_var_overrides_a_capture_default(fake, clock, cfg, repo, tmp_path):
    env = _job_env(
        fake, clock, cfg, repo, tmp_path, env_vars=["OSTIA_CAPTURE_INSTANCE_TYPE=g2-standard-16"]
    )
    assert env["OSTIA_CAPTURE_INSTANCE_TYPE"] == "g2-standard-16"


def test_failure_tests_fail(fake, clock, cfg, repo, tmp_path):
    fake.step_codes, fake.junit = {"command": 8}, "failed.xml"
    assert _drive(fake, clock, cfg, repo, tmp_path) == 1


def test_keep_on_failure_waits_then_tears_down(fake, clock, cfg, repo, tmp_path, capsys):
    fake.step_codes, fake.junit = {"command": 8}, "failed.xml"
    timeline = [*HAPPY, (900, pod_phase("Succeeded"))]
    assert _drive(fake, clock, cfg, repo, tmp_path, timeline, keep="30m") == 1
    err = capsys.readouterr().err
    assert "kubectl --context c1 --namespace ostia-test exec -it" in err
    assert not any(
        c.args[1] == ("touch", "/w/.ostia/collected") for c in fake.calls if c.verb == "exec_out"
    )
    assert 890 <= clock.monotonic() < 1000 and fake.list("job") == []


def test_keep_on_failure_is_not_used_on_success(fake, clock, cfg, repo, tmp_path):
    assert _drive(fake, clock, cfg, repo, tmp_path, keep="30m") == 0
    assert clock.monotonic() < 60


def test_ctrl_c_while_kept_tears_down_and_exits_130(fake, clock, cfg, repo, tmp_path):
    fake.step_codes, fake.junit = {"command": 8}, "failed.xml"
    original = clock.sleep

    def sleep(s):
        if clock.monotonic() > 100:
            raise KeyboardInterrupt
        original(s)

    clock.sleep = sleep
    assert _drive(fake, clock, cfg, repo, tmp_path, keep="30m") == 130
    s = _summary(tmp_path)
    assert s["teardown"] == "verified" and fake.list("job") == []


def test_failure_delete_not_confirmed(fake, clock, cfg, repo, tmp_path, capsys):
    fake.gone_after = None
    assert _drive(fake, clock, cfg, repo, tmp_path) == 4
    assert "ostia-dev remote k8s cleanup --context c1" in capsys.readouterr().err
    fake.step_codes, fake.junit = {"command": 8}, "failed.xml"
    assert _drive(fake, clock, cfg, repo, tmp_path) == 1  # the test result wins over 4


def test_second_ctrl_c_skips_the_wait_not_the_delete(fake, clock, cfg, repo, tmp_path, capsys):
    fake.gone_after = 30
    original = fake.delete
    presses = []

    def delete(*a, **kw):
        original(*a, **kw)
        presses.append(1)

    fake.delete = delete
    original_sleep = clock.sleep

    def sleep(s):
        if presses:
            raise KeyboardInterrupt  # pressed again while waiting for the objects to go
        original_sleep(s)

    def ctrl_c(k):
        raise KeyboardInterrupt  # the first Ctrl-C, while the run streams

    clock.sleep = sleep
    timeline = [(3, pod_phase("Running")), (6, log(["A"])), (7, ctrl_c)]
    code = _drive(fake, clock, cfg, repo, tmp_path, timeline)
    assert code == 130 and presses
    assert "cleanup --context c1 --namespace ostia-test --run-id" in capsys.readouterr().err


def test_gc_deletes_this_owners_expired_runs_including_suspended_jobs(
    fake, clock, cfg, repo, tmp_path
):
    owner = core.owner_id()
    for name, who, expires in [
        ("ostia-old", owner, "2026-10-01T00:00:00Z"),
        ("ostia-fresh", owner, "2026-10-03T00:00:00Z"),
        ("ostia-theirs", "someone", "2026-10-01T00:00:00Z"),
    ]:
        fake.apply(
            {
                "apiVersion": "batch/v1",
                "kind": "Job",
                "metadata": {
                    "name": name,
                    "labels": {"ostia.dev/managed": "true", "ostia.dev/owner": who},
                    "annotations": {"ostia.dev/expires": expires},
                },
                "spec": {"suspend": True},
            },
            record=False,
        )
    assert _drive(fake, clock, cfg, repo, tmp_path) == 0
    names = {j["metadata"]["name"] for j in fake.list("job")}
    assert names == {"ostia-fresh", "ostia-theirs"}


def test_env_repeat_runs_one_job_per_env(fake, clock, cfg, repo, tmp_path):
    def timeline_again(k):
        k.script(HAPPY)

    timeline = [*HAPPY, (40, timeline_again)]
    assert _drive(fake, clock, cfg, repo, tmp_path, timeline, envs=("default", "gcc11")) == 0
    jobs = [c.args[1] for c in fake.calls if c.verb == "apply" and c.args[0] == "Job"]
    assert len(set(jobs)) == 2


def test_describe_without_a_price(fake, clock, repo, tmp_path):
    cfg = config.load(tmp_path / "none.toml")
    assert _drive(fake, clock, cfg, repo, tmp_path) == 0
    s = _summary(tmp_path)
    assert s["price"] is None and s["cost_estimate"] is None
    assert s["provider"] == "gke" and s["allow_unguarded"] is False


# Review fixes (see the ledger's "Final:" lines).


def test_a_connection_that_recovers_does_not_fail_the_run(fake, clock, cfg, repo, tmp_path):
    timeline = [
        (3, pod_phase("Running")),
        (6, log(["A"])),
        (7, drop_stream()),
        (7, lose_connection(for_seconds=30)),
        (150, drop_stream()),
        (150, lose_connection(for_seconds=10)),
        (200, log(OK_LOG)),
    ]
    assert _drive(fake, clock, cfg, repo, tmp_path, timeline) == 0


def test_a_lost_connection_while_kept_still_tears_down(fake, clock, cfg, repo, tmp_path):
    fake.step_codes, fake.junit = {"command": 8}, "failed.xml"
    timeline = [*HAPPY, (100, lose_connection(for_seconds=20)), (900, pod_phase("Succeeded"))]
    assert _drive(fake, clock, cfg, repo, tmp_path, timeline, keep="30m") == 1
    assert fake.list("job") == []
    assert _summary(tmp_path)["teardown"] == "verified"


def test_failure_cache_volume_in_use(fake, clock, cfg, repo, tmp_path, capsys):
    timeline = [
        (
            3,
            event(
                "FailedAttachVolume",
                'Multi-Attach error for volume "pvc-1" Volume '
                "is already used by pod(s) ostia-other",
            ),
        )
    ]
    assert _drive(fake, clock, cfg, repo, tmp_path, timeline, cache=True) == 3
    err = capsys.readouterr().err
    assert "ostia-test-cache" in err and "cleanup --cache" in err
    assert clock.monotonic() < 60


def test_the_delete_ignores_a_second_ctrl_c(fake, clock, cfg, repo, tmp_path):
    import signal

    seen = []
    original = fake.delete

    def delete(*a, **kw):
        seen.append(signal.getsignal(signal.SIGINT))
        original(*a, **kw)

    fake.delete = delete
    assert _drive(fake, clock, cfg, repo, tmp_path) == 0
    assert seen == [signal.SIG_IGN]  # kubectl inherits it, so the DELETE is always sent
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


def test_failure_kubectl_forbidden_rbac(fake, clock, cfg, repo, tmp_path, capsys):
    from fakes.kube import forbid

    timeline = [*HAPPY, (3, forbid("create", "pods/exec"))]
    assert _drive(fake, clock, cfg, repo, tmp_path, timeline) == 2
    err = capsys.readouterr().err
    assert "create pods/exec" in err and "ostia-test-developer" in err
    assert fake.list("job") == []


def test_a_job_created_before_a_lost_connection_is_still_deleted(fake, clock, cfg, repo, tmp_path):
    from ostia_dev.remote.k8s.kube import LostConnection

    original = fake.apply

    def apply(obj, record=True):
        out = original(obj, record=record)
        if obj["kind"] == "Job" and record:
            raise LostConnection("error: the response never came back")
        return out

    fake.apply = apply
    assert _drive(fake, clock, cfg, repo, tmp_path) == 3
    assert fake.list("job") == []


def test_the_secret_is_created_not_applied(fake, clock, cfg, repo, tmp_path):
    # apply would copy stringData, in plain text, into last-applied-configuration
    _drive(fake, clock, cfg, repo, tmp_path, allow_secret=True, env_vars=["GH_TOKEN=s3cret"])
    assert [c.args[0] for c in fake.calls if c.verb == "create"] == ["Secret"]
    assert not [c for c in fake.calls if c.verb == "apply" and c.args[0] == "Secret"]


def test_an_event_without_a_message(fake, clock, cfg, repo, tmp_path):
    def bare(k):
        (pod,) = k._objs("pod")
        k.event_list.append(
            {
                "kind": "Event",
                "reason": "Pulling",
                "involvedObject": {"uid": pod["metadata"]["uid"]},
            }
        )

    assert _drive(fake, clock, cfg, repo, tmp_path, [(1, bare), *HAPPY]) == 0


TWO_OK = [(3, pod_phase("Running")), (9, log(OK_LOG, index=0)), (9, log(OK_LOG, index=1))]


def test_two_pod_happy_path(fake, clock, cfg, repo, tmp_path, capsys):
    assert _drive(fake, clock, cfg, repo, tmp_path, TWO_OK, pods=2) == 0
    order = [(c.verb, c.args[0]) for c in fake.calls if c.verb in ("apply", "create", "patch")]
    assert order == [("apply", "Job"), ("create", "Service"), ("create", "NetworkPolicy"),
                     ("patch", "job")]  # fmt: skip
    uploads = {c.args[0] for c in fake.calls if c.verb == "exec_in"}
    assert len(uploads) == 2
    ready = [c.args[0] for c in fake.calls if c.verb == "exec_out" and "ready" in str(c.args[1])]
    collected = [c for c in fake.calls if c.verb == "exec_out" and "collected" in str(c.args[1])]
    assert len(ready) == 2 and len(collected) == 2
    assert not any(c.verb == "logs_follow" for c in fake.calls)
    s = _summary(tmp_path)
    assert s["node"] == "node-1, node-2" and [r["exit"] for r in s["ranks"]] == [0, 0]
    run_dir = tmp_path / "res" / s["run_id"]
    assert (run_dir / "rank-0" / "steps.json").exists() and (
        run_dir / "rank-1" / "log.txt"
    ).exists()
    assert not (run_dir / "capture").exists() and s["capture"] is None
    run_sel = f"ostia.dev/run-id={s['run_id']}"
    for kind in ("job", "pod", "service", "networkpolicy"):
        assert fake.list(kind, selector=run_sel) == [], kind
    out = capsys.readouterr().out
    assert "[rank 0] [ostia] step install" in out and "[rank 1] [ostia] step install" in out


def test_two_pods_starting_ten_minutes_apart(fake, clock, cfg, repo, tmp_path):
    timeline = [
        (3, pod_phase("Running", index=0)),
        (600, pod_phase("Running", index=1)),
        (610, log(OK_LOG, index=0)),
        (610, log(OK_LOG, index=1)),
    ]
    assert _drive(fake, clock, cfg, repo, tmp_path, timeline, pods=2) == 0
    assert _summary(tmp_path)["phases"]["node"] >= 600


def test_failure_one_pod_oomkilled_tears_down_both(fake, clock, cfg, repo, tmp_path):
    timeline = [
        (3, pod_phase("Running")),
        (9, log(OK_LOG[:1], index=0)),
        (12, pod_phase("Failed", "OOMKilled", index=1)),
    ]
    assert _drive(fake, clock, cfg, repo, tmp_path, timeline, pods=2) == 3
    s = _summary(tmp_path)
    assert "rank 1" in s["message"] and "OOMKilled" in s["message"]
    assert clock.monotonic() < 120
    assert fake.list("job") == [] and fake.list("pod") == []


def test_failure_one_pod_never_schedules(fake, clock, cfg, repo, tmp_path):
    timeline = [
        (3, pod_phase("Running", index=0)),
        (5, event("FailedScheduling", "0/3 nodes are available: 3 Insufficient nvidia.com/gpu")),
    ]
    assert _drive(fake, clock, cfg, repo, tmp_path, timeline, pods=2) == 3
    msg = _summary(tmp_path)["message"]
    assert "rank 1" in msg and "0/3 nodes are available" in msg


def test_two_pod_logs_resume_without_duplicates(fake, clock, cfg, repo, tmp_path, capsys):
    timeline = [
        (3, pod_phase("Running")),
        (9, log(["[ostia] step a (command): x", "same-second"], index=0)),
        (9, log(["[ostia] step a (command): x"], index=1)),
        (10, lose_connection(for_seconds=7)),
        (20, log(OK_LOG, index=0)),
        (20, log(OK_LOG, index=1)),
    ]
    assert _drive(fake, clock, cfg, repo, tmp_path, timeline, pods=2) == 0
    out = capsys.readouterr().out.splitlines()
    assert out.count("[rank 0] same-second") == 1
    assert out.count("[rank 0] [ostia] step a (command): x") == 1
    assert out.count("[rank 1] [ostia] step a (command): x") == 1
    assert sum(FINISHED in line for line in out) == 2


def test_two_pod_finished_needs_both(fake, clock, cfg, repo, tmp_path, capsys):
    timeline = [
        (3, pod_phase("Running")),
        (9, log(OK_LOG, index=0)),
        (30, log(["late line", *OK_LOG], index=1)),
    ]
    assert _drive(fake, clock, cfg, repo, tmp_path, timeline, pods=2) == 0
    assert "[rank 1] late line" in capsys.readouterr().out


def test_keep_on_failure_two_pods(fake, clock, cfg, repo, tmp_path, capsys):
    fake.rank_step_codes, fake.junit = {1: {"command": 8}}, "failed.xml"
    timeline = [*TWO_OK, (900, pod_phase("Succeeded"))]
    assert _drive(fake, clock, cfg, repo, tmp_path, timeline, keep="30m", pods=2) == 1
    err = capsys.readouterr().err
    kept = [line for line in err.splitlines() if "kept for debugging" in line]
    assert len(kept) == 1 and kept[0].count("exec -it") == 2
    assert fake.list("job") == []


def test_two_pod_run_in_an_unguarded_namespace_creates_no_policy(fake, clock, cfg, repo, tmp_path):
    for p in fake.list("networkpolicy"):
        fake.delete("networkpolicy", p["metadata"]["name"])
    assert _drive(fake, clock, cfg, repo, tmp_path, TWO_OK, pods=2, allow_unguarded=True) == 0
    created = [c.args[0] for c in fake.calls if c.verb == "create"]
    assert "Service" in created and "NetworkPolicy" not in created


def test_failure_dead_rank_with_failing_logs_names_the_rank(fake, clock, cfg, repo, tmp_path):
    timeline = [
        (3, pod_phase("Running")),
        (12, logs_fail()),
        (12, pod_phase("Failed", "PreemptionByScheduler", index=1)),
    ]
    assert _drive(fake, clock, cfg, repo, tmp_path, timeline, pods=2) == 3
    msg = _summary(tmp_path)["message"]
    assert "rank 1" in msg and "preempted" in msg
    assert clock.monotonic() < 60


def test_the_rank_with_the_reason_is_blamed(fake, clock, cfg, repo, tmp_path):
    timeline = [
        (3, pod_phase("Running")),
        (12, pod_phase("Failed", index=0)),
        (12, pod_phase("Failed", "OOMKilled", index=1)),
    ]
    assert _drive(fake, clock, cfg, repo, tmp_path, timeline, pods=2) == 3
    msg = _summary(tmp_path)["message"]
    assert "rank 1" in msg and "OOMKilled" in msg
