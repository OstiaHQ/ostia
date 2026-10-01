"""A developer with only the Role and ClusterRole that `init` prints can run (RFC-0005 §4.3).

kind's own kubeconfig is cluster-admin, which hides a missing verb; this test impersonates
a user bound to exactly the printed Role and ClusterRole.
"""

import json
import os
import subprocess

import pytest
from kindlib import CONFIG, EXE, NAMESPACE, kubectl
from ostia_dev.remote.k8s import kubectl as pinned

pytestmark = pytest.mark.kind
USER = "ostia-dev"
DEV = "kind-ostia-dev"


@pytest.fixture(scope="module")
def dev_env(tmp_path_factory):
    kubectl(
        "create",
        "rolebinding",
        "ostia-test-developer",
        "--role",
        "ostia-test-developer",
        "--user",
        USER,
        check=False,
    )
    kubectl(
        "create",
        "clusterrolebinding",
        f"ostia-test-developer-{NAMESPACE}",
        "--clusterrole",
        f"ostia-test-developer-{NAMESPACE}",
        "--user",
        USER,
        namespace=None,
        check=False,
    )
    raw = json.loads(
        kubectl("config", "view", "--raw", "--minify", "-o", "json", namespace=None).stdout
    )
    raw["users"][0]["user"]["as"] = USER
    raw["users"][0]["name"] = DEV
    raw["contexts"][0].update(name=DEV)
    raw["contexts"][0]["context"]["user"] = DEV
    raw["current-context"] = DEV
    d = tmp_path_factory.mktemp("dev")
    (d / "kubeconfig").write_text(json.dumps(raw))
    (d / "config.toml").write_text(
        CONFIG.read_text() + f"\n[remote.k8s.contexts.{DEV}]\n"
        f'provider = "generic"\nnamespace = "{NAMESPACE}"\n'
    )
    return {
        **os.environ,
        "KUBECONFIG": str(d / "kubeconfig"),
        "OSTIA_CONFIG": str(d / "config.toml"),
    }


def _dev(env, *args, timeout=1500):
    return subprocess.run(
        [EXE, "remote", "k8s", *args], capture_output=True, text=True, env=env, timeout=timeout
    )


def test_the_printed_role_is_enough_for_a_run(dev_env):
    r = _dev(dev_env, "--context", DEV, "--profile", "cpu", "--no-build", "--cache", "--", "true")
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr


def test_the_printed_role_is_enough_for_cleanup_and_profiles(dev_env):
    r = _dev(dev_env, "cleanup", "--context", DEV)
    assert r.returncode == 0, r.stdout + r.stderr
    r = _dev(dev_env, "profiles", "--context", DEV)
    assert r.returncode == 0 and "cpu (generic)" in r.stdout, r.stdout + r.stderr


def test_the_role_is_not_cluster_admin(dev_env):
    r = subprocess.run(
        [str(pinned.ensure()), "--context", DEV, "get", "secrets", "-n", "kube-system"],
        capture_output=True,
        text=True,
        env=dev_env,
    )
    assert r.returncode != 0 and "forbidden" in r.stderr.lower()
