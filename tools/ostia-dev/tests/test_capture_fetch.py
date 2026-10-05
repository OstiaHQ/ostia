"""capture.fetch against an in-memory transport (RFC-0003 §4): manifest first, then only the
listed files, each a regular member requested once, verified before it is kept."""

import io
import json
import tarfile

import pytest
from fakes.capture import capture_files
from ostia_dev.errors import InfraError
from ostia_dev.remote import capture
from ostia_dev.topo import manifest


class Pod:
    """A capture directory behind read_tar(names, limit). `fail` transport errors come first;
    `special` turns a name into a non-regular member; `extra` and `dup` add members."""

    def __init__(self, tmp_path, files, *, fail=0, special=None, extra=(), dup=None, wc_lies=False):
        self.tmp, self.files, self.fail = tmp_path, dict(files), fail
        self.special, self.extra, self.dup, self.wc_lies = special or {}, extra, dup, wc_lies
        self.calls: list[list[str]] = []
        self.after_first = None

    def read_tar(self, names, limit):
        self.calls.append(list(names))
        if self.fail:
            self.fail -= 1
            raise capture.CaptureTransportError("scripted")
        if "manifest.json" not in self.files:
            raise capture.CaptureAbsent()
        if any(n not in self.files for n in names):
            raise capture.CaptureTransportError("wc -c: no such file")
        if not self.wc_lies and sum(len(self.files[n]) for n in names) > limit:
            raise capture.CaptureTooLarge("wc")
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as t:
            extra = list(self.extra) if len(names) > 1 else []
            for name in [*names, *extra, *([self.dup] if self.dup in names else [])]:
                info = tarfile.TarInfo(name)
                kind = self.special.get(name)
                if kind is None:
                    data = self.files.get(name, b"stray\n")
                    info.size = len(data)
                    t.addfile(info, io.BytesIO(data))
                    continue
                info.type = kind
                if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                    info.linkname = "/etc/hostname" if kind == tarfile.SYMTYPE else "manifest.json"
                t.addfile(info)
        path = self.tmp / f"t{len(self.calls)}.tar"
        path.write_bytes(buf.getvalue())
        if self.after_first:
            self.after_first(self)
            self.after_first = None
        return path


@pytest.fixture
def dest(tmp_path):
    return tmp_path / "res" / "capture"


def _only_diagnostics(dest):
    assert sorted(p.name for p in dest.iterdir()) == ["diagnostics.txt"]


def test_a_complete_capture_is_accepted(tmp_path, dest):
    pod = Pod(tmp_path, capture_files())
    out = capture.fetch(pod.read_tar, dest)
    assert out == capture.Accepted("complete", [])
    assert manifest.accept(dest)["status"] == "complete"
    assert (dest / "diagnostics.txt").exists()
    assert pod.calls[0] == ["manifest.json"]
    assert pod.calls[1] == ["manifest.json", "hwloc.xml", "meta.json", "nics.json", "nvml.json"]
    assert pod.calls[2] == ["diagnostics.txt"]
    assert not [p for p in dest.parent.iterdir() if p.name.startswith(".")]


def test_a_partial_capture_is_accepted_with_its_status(tmp_path, dest):
    out = capture.fetch(Pod(tmp_path, capture_files("partial")).read_tar, dest)
    assert out == capture.Accepted("partial", ["nvml.json missing (nvml_init)"])
    entry = capture.status_entry(out)
    assert entry == {"result": "accepted", "reason": None, "status": "partial",
                     "fix": "nvml.json missing (nvml_init)"}  # fmt: skip


def test_no_manifest_is_absent_and_leaves_nothing(tmp_path, dest):
    files = capture_files()
    del files["manifest.json"]
    assert capture.fetch(Pod(tmp_path, files).read_tar, dest) == capture.Absent()
    assert not dest.exists()
    assert capture.status_entry(capture.Absent())["result"] == "absent"


def _rejected(tmp_path, dest, pod) -> capture.Rejected:
    out = capture.fetch(pod.read_tar, dest)
    assert isinstance(out, capture.Rejected), out
    _only_diagnostics(dest)
    return out


def test_a_duplicate_member_is_rejected(tmp_path, dest):
    out = _rejected(tmp_path, dest, Pod(tmp_path, capture_files(), dup="nics.json"))
    assert out.reason == "the tar stream repeats a member"


def test_sanitized_false_is_rejected_with_the_leak_check_fix(tmp_path, dest):
    out = _rejected(tmp_path, dest, Pod(tmp_path, capture_files(sanitized=False)))
    assert "sanitized false" in out.reason and "fixtures.md#leak-check" in out.fix


def test_a_failed_capture_is_rejected(tmp_path, dest):
    files = capture_files(status="failed", files={}, topology_id=None)
    out = _rejected(tmp_path, dest, Pod(tmp_path, files))
    assert "status failed" in out.reason


def test_a_hash_mismatch_is_rejected(tmp_path, dest):
    files = capture_files()
    files["nics.json"] = b'{"nics": "changed"}\n'
    out = _rejected(tmp_path, dest, Pod(tmp_path, files))
    assert out.reason == "nics.json does not match its sha256 in manifest.json"
    assert "keep-on-failure" in out.fix


def test_a_symlink_member_is_rejected(tmp_path, dest):
    pod = Pod(tmp_path, capture_files(), special={"nics.json": tarfile.SYMTYPE})
    assert _rejected(tmp_path, dest, pod).reason == (
        "nics.json is not a regular file in the tar stream"
    )


@pytest.mark.parametrize("kind", [tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.DIRTYPE])
def test_hardlink_device_and_directory_members_are_rejected(tmp_path, dest, kind):
    pod = Pod(tmp_path, capture_files(), special={"hwloc.xml": kind})
    assert "is not a regular file" in _rejected(tmp_path, dest, pod).reason


def test_a_member_outside_the_list_is_rejected_without_its_name(tmp_path, dest):
    pod = Pod(tmp_path, capture_files(), extra=["node-name.example.internal"])
    out = _rejected(tmp_path, dest, pod)
    assert out.reason == "the tar stream holds 1 unrequested members"
    assert "example" not in json.dumps(capture.status_entry(out))


def test_oversize_is_rejected_before_any_transfer(tmp_path, dest, monkeypatch):
    monkeypatch.setattr(capture, "LIMIT", 100)
    out = _rejected(tmp_path, dest, Pod(tmp_path, capture_files()))
    assert "larger than" in out.reason and "/w/capture" in out.fix


def test_oversize_is_caught_in_the_stream_when_wc_lies(tmp_path, dest, monkeypatch):
    monkeypatch.setattr(capture, "LIMIT", 100)
    out = _rejected(tmp_path, dest, Pod(tmp_path, capture_files(), wc_lies=True))
    assert "larger than" in out.reason


def test_one_transport_error_is_retried(tmp_path, dest):
    pod = Pod(tmp_path, capture_files(), fail=1)
    assert capture.fetch(pod.read_tar, dest) == capture.Accepted("complete", [])
    assert pod.calls[0] == pod.calls[1] == ["manifest.json"]


def test_a_non_regular_file_rejects_without_a_retry(tmp_path, dest):
    pod = Pod(tmp_path, capture_files())
    original = pod.read_tar

    def read_tar(names, limit):
        if len(names) > 1:
            pod.calls.append(list(names))
            raise capture.CaptureNotRegular("exit 4")
        return original(names, limit)

    pod.read_tar = read_tar
    out = _rejected(tmp_path, dest, pod)
    assert out.reason.startswith("not_regular:")
    assert sum(len(c) > 1 for c in pod.calls) == 1


def test_a_local_write_failure_is_a_rejection_not_a_traceback(tmp_path, dest, monkeypatch):
    def full(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(capture.os, "replace", full)
    out = capture.fetch(Pod(tmp_path, capture_files()).read_tar, dest)
    assert isinstance(out, capture.Rejected) and out.reason.startswith("local_io:")
    assert not [p for p in dest.parent.iterdir() if p.name.startswith(".")]


def test_two_transport_errors_reject_the_capture(tmp_path, dest):
    out = capture.fetch(Pod(tmp_path, capture_files(), fail=2).read_tar, dest)
    assert isinstance(out, capture.Rejected) and "rerun" in out.fix
    assert not dest.exists() or not (dest / "manifest.json").exists()


def test_a_manifest_that_changes_between_fetches_is_rejected(tmp_path, dest):
    pod = Pod(tmp_path, capture_files())

    def rewrite(p):
        doc = json.loads(p.files["manifest.json"])
        doc["tool_version"] = "0.1.1"
        p.files["manifest.json"] = json.dumps(doc).encode()

    pod.after_first = rewrite
    out = _rejected(tmp_path, dest, pod)
    assert out.reason == "manifest.json changed during the fetch"


def test_a_missing_validator_rejects_instead_of_raising(tmp_path, dest, monkeypatch):
    def broken():
        raise InfraError("error: jsonschema is not installed")

    monkeypatch.setattr(manifest, "_validator", broken)
    out = _rejected(tmp_path, dest, Pod(tmp_path, capture_files()))
    assert "jsonschema" in out.reason


def test_describe_is_one_line_with_the_fix():
    entry = capture.status_entry(capture.Rejected("the tar stream repeats a member", "rerun"))
    assert capture.describe("node-1", entry) == (
        "capture node-1: rejected: the tar stream repeats a member; fix: rerun"
    )
    ok = capture.status_entry(capture.Accepted("complete"))
    assert capture.describe("node-0", ok) == "capture node-0: accepted (complete)"


ARN = "arn:aws:eks:us-east-1:111122223333:cluster/example-gpu"
GKE = "gke_example-gpu-project_us-central1-a_example-cluster"


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([ARN], [ARN, "111122223333", "example-gpu"]),
        ([GKE], [GKE, "example-gpu-project", "example-cluster"]),
        # region and zone names have at most two digits, so a numbered project id is kept
        (["gke_example-project-123456_us-central1-a_example-cluster"],
         ["gke_example-project-123456_us-central1-a_example-cluster", "example-project-123456",
          "example-cluster"]),
        (["gke_example-gpu-project_ap-southeast-1a_example-cluster"],
         ["gke_example-gpu-project_ap-southeast-1a_example-cluster", "example-gpu-project",
          "example-cluster"]),
        (["gke_example-gpu-project_europe-west4_example-cluster"],
         ["gke_example-gpu-project_europe-west4_example-cluster", "example-gpu-project",
          "example-cluster"]),
        (["example-context", " example-context ", ""], ["example-context"]),
        (["c1", None, "default"], ["c1", "default"]),
        (["arn:aws:eks:us-east-1:12345:cluster/abc"], ["arn:aws:eks:us-east-1:12345:cluster/abc"]),
    ],
)  # fmt: skip
def test_leak_identifiers(values, expected):
    assert capture.leak_identifiers(values) == expected
