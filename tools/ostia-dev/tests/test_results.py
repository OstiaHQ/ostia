"""Collected results: control files, the allowlist, summary.json and the summary line
(RFC-0005 §3.4; R7, R14)."""

import io
import json
import tarfile

import pytest
from ostia_dev.errors import InfraError
from ostia_dev.remote import results


def _tar(path, members):
    """members: (TarInfo kwargs, data or None)."""
    with tarfile.open(path, "w") as t:
        for kw, data in members:
            info = tarfile.TarInfo(kw.pop("name"))
            for k, v in kw.items():
                setattr(info, k, v)
            if data is not None:
                info.size = len(data)
            t.addfile(info, io.BytesIO(data) if data is not None else None)
    return path


def _file(name, data=b"x"):
    return ({"name": name}, data)


STEPS = json.dumps({"schema": 1, "state": "done", "code_wait": "ok", "exit": 0, "steps": []})


def test_control_files_are_extracted_without_a_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(results, "CAP_BYTES", 10)  # the artifact cap doesn't apply here
    tar = _tar(tmp_path / "c.tar", [
        _file("steps.json", STEPS.encode()),
        _file("log.txt", b"a" * 100),
        _file("fingerprint.txt", b"L4"),
        _file("other.txt"),
    ])  # fmt: skip
    out = tmp_path / "out"
    ctl = results.extract_control(tar, out)
    assert ctl.steps["state"] == "done"
    assert (out / "log.txt").read_bytes() == b"a" * 100
    assert (out / "fingerprint.txt").exists() and not (out / "other.txt").exists()
    assert ctl.log_truncated is False


def test_log_is_truncated_at_the_log_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(results, "LOG_CAP_BYTES", 10)
    tar = _tar(
        tmp_path / "c.tar", [_file("steps.json", STEPS.encode()), _file("log.txt", b"b" * 50)]
    )
    ctl = results.extract_control(tar, tmp_path / "out")
    assert ctl.log_truncated is True
    assert (tmp_path / "out" / "log.txt").read_bytes().startswith(b"b" * 10)


@pytest.mark.parametrize("steps", [None, b"{not json", b'{"schema": 1}'])
def test_missing_or_invalid_steps_json_is_exit_3(tmp_path, steps):
    members = [_file("log.txt")] + ([_file("steps.json", steps)] if steps is not None else [])
    tar = _tar(tmp_path / "c.tar", members)
    with pytest.raises(InfraError) as e:
        results.extract_control(tar, tmp_path / "out")
    assert e.value.code == 3 and "steps.json" in e.value.message


BUILD = "build/default/dev"


def test_artifacts_are_remapped_and_allowlisted(tmp_path):
    tar = _tar(tmp_path / "a.tar", [
        _file(f"{BUILD}/ostia-summary.txt", b"architectures: x\n"),
        _file(f"{BUILD}/junit.xml"),
        _file(f"{BUILD}/junit-off.xml"),
        _file(f"{BUILD}/Testing/Temporary/LastTest.log"),
        _file("bench/results/r1/results.jsonl"),
        _file(f"{BUILD}/CMakeCache.txt"),
        _file("src/secret.txt"),
    ])  # fmt: skip
    out = tmp_path / "out"
    dropped = results.extract_artifacts(tar, out, BUILD)
    got = sorted(str(p.relative_to(out)) for p in out.rglob("*") if p.is_file())
    assert got == [
        "Testing/Temporary/LastTest.log",
        "bench/results/r1/results.jsonl",
        "junit-off.xml",
        "junit.xml",
        "ostia-summary.txt",
    ]
    assert sorted(dropped) == [f"{BUILD}/CMakeCache.txt", "src/secret.txt"]


@pytest.mark.parametrize(
    "member",
    [
        {"name": f"{BUILD}/../../../etc/junit.xml"},  # traversal
        {"name": "/etc/junit.xml"},  # absolute
        {"name": f"{BUILD}/junit-link.xml", "type": tarfile.SYMTYPE, "linkname": "/etc/passwd"},
        {
            "name": f"{BUILD}/junit-hard.xml",
            "type": tarfile.LNKTYPE,
            "linkname": f"{BUILD}/junit.xml",
        },
        {"name": f"{BUILD}/junit-dev.xml", "type": tarfile.CHRTYPE, "devmajor": 1, "devminor": 3},
        {"name": f"{BUILD}/Testing/fifo", "type": tarfile.FIFOTYPE},
    ],
    ids=["traversal", "absolute", "symlink", "hardlink", "device", "fifo"],
)
def test_unsafe_members_are_dropped_and_listed(tmp_path, member, capsys):
    data = None if member.get("type") not in (None, tarfile.REGTYPE) else b"x"
    tar = _tar(tmp_path / "a.tar", [_file(f"{BUILD}/junit.xml"), (dict(member), data)])
    out = tmp_path / "out"
    dropped = results.extract_artifacts(tar, out, BUILD)
    assert dropped == [member["name"]]
    assert (out / "junit.xml").exists()
    assert not (tmp_path / "etc").exists()
    assert member["name"] in capsys.readouterr().err  # the warning lists it
    assert all(not p.is_symlink() for p in out.rglob("*"))


def test_the_size_cap_drops_what_is_past_it(tmp_path, monkeypatch):
    monkeypatch.setattr(results, "CAP_BYTES", 100)
    tar = _tar(tmp_path / "a.tar", [
        _file(f"{BUILD}/junit-a.xml", b"a" * 60),
        _file(f"{BUILD}/junit-b.xml", b"b" * 60),
        _file(f"{BUILD}/junit-c.xml", b"c" * 30),
    ])  # fmt: skip
    out = tmp_path / "out"
    dropped = results.extract_artifacts(tar, out, BUILD)
    assert dropped == [f"{BUILD}/junit-b.xml"]
    assert (out / "junit-a.xml").exists() and (out / "junit-c.xml").exists()


def test_bench_results_are_copied_for_compare(tmp_path):
    out = tmp_path / "out"
    (out / "bench" / "results" / "rid").mkdir(parents=True)
    (out / "bench" / "results" / "rid" / "results.jsonl").write_text("{}\n")
    repo = tmp_path / "repo"
    copied = results.copy_bench_results(out, repo, "rid")
    assert copied == repo / "bench" / "results" / "rid"
    assert (copied / "results.jsonl").read_text() == "{}\n"
    assert results.copy_bench_results(tmp_path / "none", repo, "rid") is None


def _summary(**over):
    s = {
        "run_id": "k8s-l4-20261002-141501-a1b2c3",
        "backend": "k8s",
        "profile": "l4",
        "suite": "gpu",
        "env": "cuda-12",
        "gpu": "L4 (8.9) driver 580.95",
        "git_sha": "3f2a9c1+dirty",
        "tree_hash": "9ab3" + "0" * 60,
        "result": "passed",
        "exit_code": 0,
        "seconds": 18 * 60 + 12,
        "phases": {"upload": 4, "node": 220},
        "steps": [
            {"name": "install", "kind": "install", "code": 0, "seconds": 302},
            {"name": "configure", "kind": "build", "code": 0, "seconds": 91},
            {"name": "build", "kind": "build", "code": 0, "seconds": 300},
            {"name": "test", "kind": "command", "code": 0, "seconds": 175},
        ],
        "reports": {},
    }
    s.update(over)
    return s


def test_summary_line():
    lines = results.summary_lines(_summary())
    assert lines[0] == (
        "k8s-l4-20261002-141501-a1b2c3  passed  L4 (8.9) driver 580.95  "
        "sha 3f2a9c1+dirty(tree 9ab3…)  suite gpu  18m12s"
    )
    assert lines[1] == "  upload 4s · node 3m40s · install 5m02s · build 6m31s · test 2m55s"
    assert lines[2].startswith("  tip: install and build took 63% of this run; --cache")


def test_no_tip_when_install_and_build_are_small():
    s = _summary(steps=[{"name": "test", "kind": "command", "code": 0, "seconds": 900}])
    assert not any("tip" in line for line in results.summary_lines(s))


def test_container_summary_has_no_gpu_and_no_cache_tip():
    s = _summary(
        backend="container",
        run_id="container-cpu-x",
        profile="cpu",
        gpu=None,
        suite=None,
        env="default",
    )
    lines = results.summary_lines(s)
    assert (
        lines[0]
        == "container-cpu-x  passed  cpu  sha 3f2a9c1+dirty(tree 9ab3…)  env default  18m12s"
    )
    assert not any("--cache" in line for line in lines)


def test_report_steps_are_shown():
    s = _summary(reports={"aa": {"code": 1, "noise_floor": "±0.812%"}})
    assert any(
        line == "  report aa: noise floor ±0.812% (exit 1, never fails the run)"
        for line in results.summary_lines(s)
    )


def test_noise_floor_is_parsed_from_the_log():
    log = "target: l4\nA/A noise floor: ±0.312% (must be ≤ 0.5%)\n"
    assert results.parse_noise_floor(log) == "±0.312%"
    assert results.parse_noise_floor("nothing") is None


def test_write_summary(tmp_path):
    s = _summary()
    path = results.write_summary(tmp_path, s)
    assert json.loads(path.read_text()) == s
