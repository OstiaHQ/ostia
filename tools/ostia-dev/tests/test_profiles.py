"""Node profiles: merging, providers, parallelism (RFC-0005 §3.2, §4.4)."""

import pytest

from ostia_dev import config
from ostia_dev.errors import UsageError
from ostia_dev.remote import profiles


@pytest.fixture
def cfg(tmp_path):
    return config.load(tmp_path / "none.toml")


def _cfg_with(tmp_path, text):
    path = tmp_path / "c.toml"
    path.write_text("schema = 1\n" + text)
    return config.load(path)


def test_builtins_have_the_rfc_profiles(cfg):
    assert set(cfg.profiles) == {"l4", "a100", "h100", "cpu"}
    assert set(cfg.suites) == {"gpu", "sanitizer", "bench-smoke", "overhead-aa", "cpu"}
    assert cfg.image.startswith("ghcr.io/prefix-dev/pixi:0.81.0-noble@sha256:")
    assert "aks" not in cfg.profiles["l4"]  # Azure has no generally available L4 size


def test_resolve_l4_on_gke(cfg):
    p = profiles.resolve("l4", "gke", cfg)
    assert (p.kind, p.gpus, p.compute_capability) == ("gpu", 1, "8.9")
    assert (p.cpu, p.memory, p.ephemeral_storage) == ("6", "24Gi", "60Gi")
    assert p.node_selector == {"cloud.google.com/gke-accelerator": "nvidia-l4"}
    assert p.tolerations == [{"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}]
    assert p.env["LD_LIBRARY_PATH"] == "/usr/local/nvidia/lib64"
    assert p.is_gpu


def test_user_fields_merge_per_key(tmp_path):
    cfg = _cfg_with(
        tmp_path,
        '[remote.k8s.profiles.l4]\nmemory = "20Gi"\n'
        '[remote.k8s.profiles.l4.gke]\nephemeral_storage = "80Gi"\n',
    )
    gke = profiles.resolve("l4", "gke", cfg)
    assert gke.ephemeral_storage == "80Gi"  # the provider table overrides the base field
    assert gke.memory == "20Gi" and gke.cpu == "6"
    assert gke.node_selector == {"cloud.google.com/gke-accelerator": "nvidia-l4"}
    assert profiles.resolve("l4", "eks", cfg).ephemeral_storage == "60Gi"


def test_unknown_profile_is_exit_2_listing_the_known(cfg):
    with pytest.raises(UsageError) as e:
        profiles.resolve("t4", "gke", cfg)
    assert "a100, cpu, h100, l4" in e.value.message


def test_gpu_profile_without_a_provider_mapping_is_exit_2(cfg):
    with pytest.raises(UsageError) as e:
        profiles.resolve("l4", "aks", cfg)
    assert "[remote.k8s.profiles.l4.aks]" in e.value.message


def test_cpu_profile_needs_no_provider_mapping(cfg):
    assert profiles.resolve("cpu", "gke", cfg).node_selector == {}


def test_generic_provider_without_node_selector_is_exit_2(cfg):
    with pytest.raises(UsageError) as e:
        profiles.resolve("cpu", "generic", cfg)
    assert e.value.code == 2
    assert "node_selector" in e.value.message


def test_generic_provider_with_node_selector(tmp_path):
    cfg = _cfg_with(
        tmp_path, '[remote.k8s.profiles.cpu.generic]\nnode_selector = { "kubernetes.io/os" = "linux" }\n'
    )
    assert profiles.resolve("cpu", "generic", cfg).node_selector == {"kubernetes.io/os": "linux"}


def test_arch_adds_the_arch_label(tmp_path):
    cfg = _cfg_with(tmp_path, '[remote.k8s.profiles.cpu]\narch = "arm64"\n')
    p = profiles.resolve("cpu", "gke", cfg)
    assert p.arch == "arm64"
    assert p.node_selector == {"kubernetes.io/arch": "arm64"}


def test_container_backend_resolves_without_a_provider(cfg):
    p = profiles.resolve("cpu", None, cfg)
    assert p.node_selector == {} and p.cpu == "4"


@pytest.mark.parametrize(
    ("cpu", "memory", "env", "expected"),
    [
        ("6", "24Gi", "cuda-12", (6, 6)),  # L4: 24 GiB allows 6 nvcc jobs
        ("6", "24Gi", "default", (6, 6)),
        ("8", "16Gi", "cuda-13", (4, 8)),  # capped at one nvcc job per 4 GiB
        ("8", "16Gi", "default", (8, 8)),  # no cap without CUDA
        ("2", "2Gi", "cuda-12", (1, 2)),  # at least one job
        ("1500m", "6Gi", "default", (1, 1)),
    ],
)
def test_parallelism(cfg, cpu, memory, env, expected):
    p = profiles.resolve("cpu", None, cfg)
    p.cpu, p.memory = cpu, memory
    assert profiles.parallelism(p, env) == expected


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("10m", 600), ("600s", 600), ("2h", 7200), ("90", 90), ("1h30m", 5400)],
)
def test_parse_duration(text, seconds):
    assert profiles.parse_duration(text) == seconds


def test_parse_duration_rejects_garbage():
    with pytest.raises(UsageError):
        profiles.parse_duration("ten minutes")


@pytest.mark.parametrize(("q", "b"), [("24Gi", 24 * 1024**3), ("512Mi", 512 * 1024**2), ("1G", 10**9)])
def test_parse_bytes(q, b):
    assert profiles.parse_bytes(q) == b
