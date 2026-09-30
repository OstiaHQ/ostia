"""Config loading, merging and writing (RFC-0005 §1.2; R8)."""

import tomllib

import pytest
from ostia_dev import config
from ostia_dev.errors import UsageError

BUILTINS = {
    "schema": 1,
    "image": "img@sha256:0",
    "windows": {"code_wait": "10m", "collect": "10m"},
    "profiles": {
        "l4": {
            "kind": "gpu",
            "cpu": "6",
            "ephemeral_storage": "60Gi",
            "gke": {"node_selector": {"cloud.google.com/gke-accelerator": "nvidia-l4"}},
        },
        "cpu": {"kind": "cpu", "cpu": "4"},
    },
    "suites": {"cpu": {"description": "build and ctest -L cpu"}},
}


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_path_order(tmp_path, monkeypatch):
    monkeypatch.delenv("OSTIA_CONFIG", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert config.user_path() == tmp_path / "home" / ".config" / "ostia" / "config.toml"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert config.user_path() == tmp_path / "xdg" / "ostia" / "config.toml"
    monkeypatch.setenv("OSTIA_CONFIG", str(tmp_path / "explicit.toml"))
    assert config.user_path() == tmp_path / "explicit.toml"


def test_missing_file_gives_the_builtins_only(tmp_path):
    cfg = config.load(tmp_path / "none.toml", builtins=BUILTINS)
    assert cfg.user == {}
    assert cfg.profiles == BUILTINS["profiles"]
    assert cfg.windows == {"code_wait": "10m", "collect": "10m"}
    assert cfg.image == "img@sha256:0"


def test_user_keys_merge_per_key_over_the_builtins(tmp_path):
    path = _write(
        tmp_path / "c.toml",
        """
schema = 1
[remote.windows]
code_wait = "60s"
[remote.k8s.profiles.l4.gke]
ephemeral_storage = "80Gi"
[remote.k8s.profiles.a100x4]
kind = "gpu"
gpus = 4
""",
    )
    cfg = config.load(path, builtins=BUILTINS)
    l4 = cfg.profiles["l4"]
    assert l4["gke"]["ephemeral_storage"] == "80Gi"  # the override
    assert l4["gke"]["node_selector"] == {"cloud.google.com/gke-accelerator": "nvidia-l4"}
    assert l4["ephemeral_storage"] == "60Gi" and l4["cpu"] == "6"  # untouched
    assert cfg.profiles["a100x4"] == {"kind": "gpu", "gpus": 4}  # an addition
    assert cfg.windows == {"code_wait": "60s", "collect": "10m"}
    assert BUILTINS["profiles"]["l4"]["gke"].get("ephemeral_storage") is None  # not mutated


@pytest.mark.parametrize("text", ["schema = 2\n", "x = 1\n"])
def test_unknown_or_missing_schema_is_rejected(tmp_path, text):
    path = _write(tmp_path / "c.toml", text)
    with pytest.raises(UsageError) as e:
        config.load(path, builtins=BUILTINS)
    assert e.value.code == 2
    assert "supported schema versions: 1" in e.value.message
    assert str(path) in e.value.message


def test_invalid_toml_is_a_usage_error(tmp_path):
    path = _write(tmp_path / "c.toml", "schema = \n")
    with pytest.raises(UsageError) as e:
        config.load(path, builtins=BUILTINS)
    assert str(path) in e.value.message


def test_context_lookup(tmp_path):
    path = _write(
        tmp_path / "c.toml",
        'schema = 1\n[remote.k8s.contexts.gke_p_z_c]\nprovider = "gke"\n',
    )
    cfg = config.load(path, builtins=BUILTINS)
    assert cfg.context("gke_p_z_c") == {"provider": "gke"}
    assert cfg.context("other") == {}


def test_append_creates_the_file_with_a_schema(tmp_path):
    path = tmp_path / "new" / "config.toml"
    config.append_table(path, ("remote", "k8s", "contexts", "c1"), {"provider": "gke"})
    doc = tomllib.loads(path.read_text())
    assert doc == {"schema": 1, "remote": {"k8s": {"contexts": {"c1": {"provider": "gke"}}}}}


def test_append_keeps_comments_and_existing_tables(tmp_path):
    original = (
        "# my settings\nschema = 1\n\n"
        '[remote.k8s.profiles.l4.gke]  # bigger disk\nephemeral_storage = "80Gi"\n'
    )
    path = _write(tmp_path / "c.toml", original)
    config.append_table(path, ("remote", "k8s", "contexts", "c1"), {"provider": "eks"})
    text = path.read_text()
    assert text.startswith(original)
    doc = tomllib.loads(text)
    assert doc["remote"]["k8s"]["profiles"]["l4"]["gke"]["ephemeral_storage"] == "80Gi"
    assert doc["remote"]["k8s"]["contexts"]["c1"] == {"provider": "eks"}


def test_append_adds_keys_to_an_existing_table(tmp_path):
    path = _write(
        tmp_path / "c.toml",
        'schema = 1\n[remote.k8s.contexts.c1]\nprovider = "gke"\n\n[other]\nx = 1\n',
    )
    config.append_table(path, ("remote", "k8s", "contexts", "c1"), {"namespace": "ostia-test"})
    doc = tomllib.loads(path.read_text())
    assert doc["remote"]["k8s"]["contexts"]["c1"] == {"provider": "gke", "namespace": "ostia-test"}
    assert doc["other"] == {"x": 1}


@pytest.mark.parametrize(
    "context",
    [
        "arn:aws:eks:us-east-1:123456789012:cluster/ostia",  # EKS
        "gke_my-project_us-central1-a_ostia",  # GKE
        'odd"name\\with.dots',  # a quote, a backslash and dots
    ],
)
def test_context_names_round_trip(tmp_path, context):
    path = tmp_path / "c.toml"
    config.append_table(
        path,
        ("remote", "k8s", "contexts", context),
        {"provider": "eks", "namespace": 'ns"1', "kubectl": "C:\\bin\\kubectl"},
    )
    cfg = config.load(path, builtins=BUILTINS)
    assert cfg.context(context) == {
        "provider": "eks",
        "namespace": 'ns"1',
        "kubectl": "C:\\bin\\kubectl",
    }


def test_append_rolls_back_when_the_result_does_not_parse(tmp_path):
    original = 'schema = 1\n[remote.k8s.contexts.c1]\nprovider = "gke"\n'
    path = _write(tmp_path / "c.toml", original)
    with pytest.raises(UsageError):  # a duplicate key makes the file invalid
        config.append_table(path, ("remote", "k8s", "contexts", "c1"), {"provider": "eks"})
    assert path.read_text() == original


def test_append_rejects_odd_value_keys(tmp_path):
    with pytest.raises(ValueError):
        config.append_table(tmp_path / "c.toml", ("t",), {"a b": "x"})
