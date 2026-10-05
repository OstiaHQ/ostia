"""The capture fetch's shell scripts, run for real under /bin/sh (RFC-0003 §4).

The k8s backend runs exactly these strings in the pod; here the capture directory is a temp dir.
"""

import io
import os
import subprocess
import tarfile

import pytest
from ostia_dev.remote import capture


def _sh(script: str, *names: str) -> subprocess.CompletedProcess:
    return subprocess.run(["/bin/sh", "-c", script, "sh", *names], capture_output=True)


@pytest.fixture
def d(tmp_path):
    out = tmp_path / "capture"
    out.mkdir()
    (out / "manifest.json").write_bytes(b"{}\n")
    (out / "hwloc.xml").write_bytes(b"x" * 1000)
    return out


def test_the_scripts_name_the_pods_capture_directory():
    assert capture.probe_script() == "test -f /w/capture/manifest.json || exit 3"
    assert capture.sizes_script() == (
        'cd /w/capture && for f; do [ -f "$f" ] && [ ! -h "$f" ] || exit 4; done; wc -c -- "$@"'
    )
    assert capture.tar_script() == 'cd /w/capture && tar -cf - -- "$@"'


def test_probe_passes_when_the_manifest_exists(d):
    assert _sh(capture.probe_script(str(d))).returncode == 0


def test_probe_exits_3_without_a_manifest(d):
    (d / "manifest.json").unlink()
    assert _sh(capture.probe_script(str(d))).returncode == 3


def test_probe_exits_3_when_the_directory_is_missing(tmp_path):
    assert _sh(capture.probe_script(str(tmp_path / "nothing"))).returncode == 3


def test_sizes_are_reported_and_summed(d):
    names = ["manifest.json", "hwloc.xml"]
    r = _sh(capture.sizes_script(str(d)), *names)
    assert r.returncode == 0
    assert capture.parse_sizes(r.stdout.decode(), names) == 1003


def test_one_size_has_no_total_line(d):
    r = _sh(capture.sizes_script(str(d)), "hwloc.xml")
    assert r.returncode == 0 and capture.parse_sizes(r.stdout.decode(), ["hwloc.xml"]) == 1000


def test_sizes_exit_4_on_a_missing_file(d):
    assert _sh(capture.sizes_script(str(d)), "hwloc.xml", "nvml.json").returncode == 4


def test_sizes_exit_4_on_a_symlink_without_following_it(d):
    (d / "nics.json").symlink_to("hwloc.xml")
    r = _sh(capture.sizes_script(str(d)), "hwloc.xml", "nics.json")
    assert r.returncode == 4 and r.stdout == b""


def test_sizes_exit_4_on_a_fifo_instead_of_blocking(d):
    os.mkfifo(d / "nvml.json")
    r = subprocess.run(
        ["/bin/sh", "-c", capture.sizes_script(str(d)), "sh", "nvml.json"],
        capture_output=True,
        timeout=10,
    )
    assert r.returncode == 4


def test_sizes_exit_4_on_a_directory(d):
    (d / "links.json").mkdir()
    assert _sh(capture.sizes_script(str(d)), "links.json").returncode == 4


def test_tar_holds_exactly_the_named_files(d):
    r = _sh(capture.tar_script(str(d)), "manifest.json", "hwloc.xml")
    assert r.returncode == 0
    with tarfile.open(fileobj=io.BytesIO(r.stdout)) as t:
        members = {m.name: m for m in t.getmembers()}
    assert set(members) == {"manifest.json", "hwloc.xml"}
    assert all(m.isreg() for m in members.values()) and members["hwloc.xml"].size == 1000


def test_tar_exits_non_zero_on_a_missing_listed_file(d):
    assert _sh(capture.tar_script(str(d)), "manifest.json", "nvml.json").returncode != 0


def test_a_symlink_is_archived_as_a_symlink(d):
    (d / "nics.json").symlink_to("manifest.json")
    r = _sh(capture.tar_script(str(d)), "nics.json")
    with tarfile.open(fileobj=io.BytesIO(r.stdout)) as t:
        (member,) = t.getmembers()
    assert member.issym()  # so fetch rejects it rather than copying the target


def test_a_dash_name_is_not_an_option(d):
    (d / "-v").write_bytes(b"x")
    assert _sh(capture.sizes_script(str(d)), "-v").returncode == 0
    assert _sh(capture.tar_script(str(d)), "-v").returncode == 0


def test_parse_sizes_rejects_short_or_bad_output():
    with pytest.raises(capture.CaptureTransportError):
        capture.parse_sizes("12 a\n", ["a", "b"])
    with pytest.raises(capture.CaptureTransportError):
        capture.parse_sizes("wc: a: No such file\n", ["a"])
