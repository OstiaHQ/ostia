"""The upload tarball, built on the host (RFC-0005 §4.10; R15)."""

import io
import tarfile

import pytest
from conftest import git
from ostia_dev.errors import UsageError
from ostia_dev.remote import tarball


def _members(tb: tarball.Tarball) -> dict[str, tarfile.TarInfo]:
    with tarfile.open(tb.path) as t:
        return {m.name: m for m in t.getmembers()}


def test_tracked_untracked_ignored_and_deleted(git_repo):
    (git_repo / "new.txt").write_text("untracked\n")  # untracked, not ignored: in
    (git_repo / "build" / "default").mkdir(parents=True)
    (git_repo / "build" / "default" / "x.o").write_text("obj")  # ignored by the real .gitignore
    (git_repo / "src" / "a.cpp").unlink()  # tracked but deleted locally: out
    tb = tarball.build(git_repo)
    assert sorted(_members(tb)) == [".gitignore", "README.md", "new.txt", "tools/run.sh"]
    assert tb.files == [".gitignore", "README.md", "new.txt", "tools/run.sh"]
    assert tb.file_count == 4
    assert tb.size == tb.path.stat().st_size


def test_entries_are_normalised(git_repo):
    m = _members(tarball.build(git_repo))
    assert all(i.mtime == 0 and i.uid == 0 and i.gid == 0 for i in m.values())
    assert all(i.uname == "" and i.gname == "" for i in m.values())
    assert m["tools/run.sh"].mode == 0o755
    assert m["README.md"].mode == 0o644


def test_same_tree_same_hash(git_repo, tmp_path):
    a = tarball.build(git_repo)
    b = tarball.build(git_repo)
    assert a.tree_hash == b.tree_hash and len(a.tree_hash) == 64
    (git_repo / "README.md").write_text("changed\n")
    assert tarball.build(git_repo).tree_hash != a.tree_hash


def test_dot_env_is_ignored_by_the_real_gitignore(git_repo):
    (git_repo / ".env").write_text("TOKEN=x\n")
    (git_repo / ".env.local").write_text("TOKEN=x\n")
    tb = tarball.build(git_repo)
    assert ".env" not in tb.files and ".env.local" not in tb.files


SECRETS = [
    "prod.pem",
    "tls.key",
    "cert.p12",
    "id_rsa",
    "id_rsa.pub",
    "id_ed25519",
    ".netrc",
    ".pypirc",
    ".npmrc",
    "my-kubeconfig.yaml",
    ".kube/config",
    "sub/.env",
    "sub/.env.prod",
]


@pytest.mark.parametrize("name", SECRETS)
def test_untracked_secret_looking_files_are_skipped_and_listed(git_repo, capsys, name):
    path = git_repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("secret\n")
    # The root .gitignore covers .env; a nested one that re-includes it tests the denylist
    (git_repo / "sub").mkdir(exist_ok=True)
    (git_repo / "sub" / ".gitignore").write_text("!.env\n!.env.*\n")
    tb = tarball.build(git_repo)
    assert name not in tb.files
    assert name in tb.skipped
    assert name in capsys.readouterr().err


def test_tracked_secret_looking_files_are_kept(git_repo):
    (git_repo / "tests").mkdir()
    (git_repo / "tests" / "fixture.pem").write_text("public test cert\n")
    git(git_repo, "add", "tests/fixture.pem")
    git(git_repo, "commit", "-q", "-m", "fixture")
    tb = tarball.build(git_repo)
    assert "tests/fixture.pem" in tb.files and tb.skipped == []


def test_large_untracked_file_warns(git_repo, capsys, monkeypatch):
    monkeypatch.setattr(tarball, "WARN_BYTES", 1000)
    (git_repo / "big.bin").write_bytes(b"x" * 2000)
    tb = tarball.build(git_repo)
    assert "big.bin" in tb.files
    err = capsys.readouterr().err
    assert "warning" in err and "big.bin" in err


def test_oversized_upload_is_refused_listing_the_largest(git_repo, monkeypatch):
    monkeypatch.setattr(tarball, "MAX_BYTES", 5000)
    (git_repo / "big1.bin").write_bytes(b"x" * 4000)
    (git_repo / "big2.bin").write_bytes(b"x" * 3000)
    with pytest.raises(UsageError) as e:
        tarball.build(git_repo)
    msg = e.value.message
    assert e.value.code == 2
    assert msg.index("big1.bin") < msg.index("big2.bin")


def test_works_in_a_git_worktree(git_worktree):
    (git_worktree / "wt-only.txt").write_text("x\n")
    assert (git_worktree / ".git").is_file()  # a gitdir: file, as in a real worktree
    tb = tarball.build(git_worktree)
    assert "wt-only.txt" in tb.files and "README.md" in tb.files
    assert ".git" not in tb.files


@pytest.fixture
def origin(git_repo, tmp_path):
    """git_repo pushed to a bare "origin", with a commit and a pull request head there."""
    bare = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    git(git_repo, "remote", "add", "origin", str(bare))
    git(git_repo, "push", "-q", "origin", "main")
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", str(bare), str(other))
    (other / "pushed.txt").write_text("from origin\n")
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", "pushed")
    sha = git(other, "rev-parse", "HEAD").strip()
    git(other, "push", "-q", "origin", "HEAD:refs/heads/feature")
    (other / "pr.txt").write_text("pr head\n")
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", "pr")
    pr_sha = git(other, "rev-parse", "HEAD").strip()
    git(other, "push", "-q", "origin", "HEAD:refs/pull/7/head")
    return sha, pr_sha


def test_ref_sha_archives_that_commit(git_repo, origin):
    sha, _ = origin
    (git_repo / "local-only.txt").write_text("not in the ref\n")
    tb = tarball.build(git_repo, ref=sha)
    assert "pushed.txt" in tb.files and "local-only.txt" not in tb.files
    assert tb.ref_sha == sha
    assert _members(tb)["tools/run.sh"].mode == 0o755
    assert all(m.mtime == 0 for m in _members(tb).values())


def test_ref_pull_request_archives_its_head(git_repo, origin):
    _, pr_sha = origin
    tb = tarball.build(git_repo, ref="pr/7")
    assert "pr.txt" in tb.files and tb.ref_sha == pr_sha


def test_ref_that_cannot_be_fetched_is_exit_2(git_repo, origin):
    with pytest.raises(UsageError) as e:
        tarball.build(git_repo, ref="0" * 40)
    assert "git fetch" in e.value.message


def test_tar_is_readable_by_plain_tar(git_repo):
    tb = tarball.build(git_repo)
    data = tb.path.read_bytes()
    with tarfile.open(fileobj=io.BytesIO(data)) as t:
        assert t.extractfile("README.md").read() == b"hello\n"
