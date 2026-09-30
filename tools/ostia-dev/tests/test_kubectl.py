"""The sha-pinned official kubectl (RFC-0005 §4.1; ostia-dev PR A Decision 2)."""

import hashlib
import io
import re
import tomllib
from importlib import resources

import pytest
from ostia_dev.errors import UsageError
from ostia_dev.remote.k8s import kubectl

BINARY = b"\x7fELF fake kubectl"
SHA = hashlib.sha256(BINARY).hexdigest()


@pytest.fixture
def table(monkeypatch):
    monkeypatch.setattr(kubectl, "platform_key", lambda: "darwin-arm64")
    monkeypatch.setattr(
        kubectl, "_table", lambda: {"version": "v1.36.5", "sha256": {"darwin-arm64": SHA}}
    )


class Fetch:
    def __init__(self, data: bytes = BINARY, fail_after: int | None = None):
        self.data, self.fail_after, self.urls = data, fail_after, []

    def __call__(self, url: str):
        self.urls.append(url)
        if self.fail_after is None:
            return io.BytesIO(self.data)
        raw = io.BytesIO(self.data[: self.fail_after])

        class Broken:
            def read(self, n=-1):
                chunk = raw.read(n)
                if not chunk:
                    raise ConnectionResetError("dropped")
                return chunk

        return Broken()


def test_download_verifies_and_caches(tmp_path, table):
    fetch = Fetch()
    path = kubectl.ensure(cache_root=tmp_path, fetch=fetch)
    assert path == tmp_path / "v1.36.5" / "kubectl"
    assert path.read_bytes() == BINARY and path.stat().st_mode & 0o777 == 0o755
    assert fetch.urls == ["https://dl.k8s.io/release/v1.36.5/bin/darwin/arm64/kubectl"]
    again = Fetch()
    assert kubectl.ensure(cache_root=tmp_path, fetch=again) == path
    assert again.urls == []  # a verified cache hit makes no request


def test_sha_mismatch_is_exit_2_and_nothing_is_kept(tmp_path, table):
    with pytest.raises(UsageError) as e:
        kubectl.ensure(cache_root=tmp_path, fetch=Fetch(b"tampered"))
    assert e.value.code == 2
    assert SHA in e.value.message and hashlib.sha256(b"tampered").hexdigest() in e.value.message
    assert not any(p.is_file() for p in tmp_path.rglob("*"))


def test_a_corrupt_cache_is_downloaded_again(tmp_path, table):
    (tmp_path / "v1.36.5").mkdir()
    (tmp_path / "v1.36.5" / "kubectl").write_bytes(b"truncated")
    fetch = Fetch()
    assert kubectl.ensure(cache_root=tmp_path, fetch=fetch).read_bytes() == BINARY
    assert len(fetch.urls) == 1


def test_a_dropped_download_leaves_no_file(tmp_path, table):
    with pytest.raises(UsageError):
        kubectl.ensure(cache_root=tmp_path, fetch=Fetch(fail_after=4))
    assert not any(p.is_file() for p in tmp_path.rglob("*"))


def test_the_flag_wins_then_the_config(tmp_path, table):
    flag = tmp_path / "flag-kubectl"
    flag.write_text("#!/bin/sh\n")
    configured = tmp_path / "cfg-kubectl"
    configured.write_text("#!/bin/sh\n")
    fetch = Fetch()
    assert kubectl.ensure(flag=str(flag), configured=str(configured), fetch=fetch) == flag
    assert kubectl.ensure(configured=str(configured), fetch=fetch) == configured
    assert fetch.urls == []


def test_a_missing_flag_path_is_exit_2(tmp_path, table):
    with pytest.raises(UsageError) as e:
        kubectl.ensure(flag=str(tmp_path / "nope"))
    assert "--kubectl" in e.value.message


@pytest.mark.parametrize(
    ("system", "machine", "key"),
    [
        ("Linux", "x86_64", "linux-amd64"),
        ("Linux", "aarch64", "linux-arm64"),
        ("Darwin", "arm64", "darwin-arm64"),
    ],
)
def test_platform_key(monkeypatch, system, machine, key):
    monkeypatch.setattr(kubectl.platform, "system", lambda: system)
    monkeypatch.setattr(kubectl.platform, "machine", lambda: machine)
    assert kubectl.platform_key() == key


def test_an_unsupported_platform_is_exit_2(monkeypatch):
    monkeypatch.setattr(kubectl.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(kubectl.platform, "machine", lambda: "x86_64")
    with pytest.raises(UsageError) as e:
        kubectl.platform_key()
    assert "--kubectl PATH" in e.value.message


def test_the_table_is_complete():
    doc = tomllib.loads((resources.files("ostia_dev.remote.k8s") / "kubectl.toml").read_text())
    assert re.fullmatch(r"v1\.\d+\.\d+", doc["version"])
    assert set(doc["sha256"]) == {"linux-amd64", "linux-arm64", "darwin-arm64"}
    assert all(re.fullmatch(r"[0-9a-f]{64}", v) for v in doc["sha256"].values())
    assert doc["version"] == kubectl.VERSION


def test_update_shas_rewrites_the_table(tmp_path):
    path = tmp_path / "kubectl.toml"
    path.write_text('version = "v1.36.9"\n\n[sha256]\nlinux-amd64 = "old"\n')
    served = {
        f"https://dl.k8s.io/release/v1.36.9/bin/{p}/kubectl.sha256": (c * 64).encode()
        for p, c in (("linux/amd64", "a"), ("linux/arm64", "b"), ("darwin/arm64", "c"))
    }
    got = kubectl.update_shas(path, fetch=lambda url: io.BytesIO(served[url]))
    assert got == {"linux-amd64": "a" * 64, "linux-arm64": "b" * 64, "darwin-arm64": "c" * 64}
    doc = tomllib.loads(path.read_text())
    assert doc == {"version": "v1.36.9", "sha256": got}
    assert path.read_text().startswith("# The official kubectl")
