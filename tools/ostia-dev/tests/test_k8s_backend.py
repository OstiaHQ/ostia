"""The single-pod k8s lifecycle against FakeKube (RFC-0005 §4.2, §4.5, §4.8; R4, R13).

Runtime rows of the RFC's Failure-handling table are `test_failure_<row>`.
"""

import json
from pathlib import Path

import pytest
from fakes.clock import FakeClock
from fakes.kube import FakeKube, drop_stream, event, exec_result, log, lose_connection, pod_phase
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
    fake, clock, cfg, repo, tmp_path, timeline=HAPPY, *, envs=("default",), profile="cpu", **extra
):
    target = Target(context="c1", namespace="ostia-test", provider="gke", kubectl=Path("/k"))
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
    calls = [(c.verb, c.args[:2]) for c in fake.calls if c.verb in ("apply", "patch", "delete")]
    job = f"ostia-{_summary(tmp_path)['run_id']}"
    assert calls[:3] == [
        ("apply", ("Job", job)),
        ("apply", ("Secret", f"{job}-env")),
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
    assert ".ostia && tar" in outs[-3][2] and "tar -cf" in outs[-2][2]
    assert outs[-1] == ("touch", "/w/.ostia/collected")
    run_dir = tmp_path / "res" / _summary(tmp_path)["run_id"]
    assert (run_dir / "junit.xml").exists() and (run_dir / "log.txt").exists()


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
