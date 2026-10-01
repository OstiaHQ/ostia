"""Server-side admission of every golden manifest (R12): PSA restricted and the schema."""

from pathlib import Path

import pytest
import yaml
from kindlib import kubectl

pytestmark = pytest.mark.kind
GOLDEN = Path(__file__).resolve().parents[1] / "golden" / "manifests"
DRYRUN = "ostia-dryrun"


@pytest.fixture(scope="module", autouse=True)
def namespace():
    kubectl("create", "namespace", DRYRUN, namespace=None, check=False)
    kubectl(
        "label",
        "namespace",
        DRYRUN,
        "pod-security.kubernetes.io/enforce=restricted",
        "--overwrite",
        namespace=None,
    )
    yield
    kubectl("delete", "namespace", DRYRUN, "--wait=false", namespace=None, check=False)


@pytest.mark.parametrize("golden", sorted(p.name for p in GOLDEN.glob("*.yaml")))
def test_the_api_server_admits_the_golden(golden):
    docs = list(yaml.safe_load_all((GOLDEN / golden).read_text()))
    for doc in docs:
        if doc.get("kind") not in ("Namespace", "ClusterRole"):
            doc["metadata"]["namespace"] = DRYRUN
        if doc.get("kind") == "Namespace":
            doc["metadata"]["name"] = DRYRUN
    text = yaml.safe_dump_all(docs)
    r = kubectl("apply", "--dry-run=server", "-f", "-", input=text, namespace=DRYRUN, check=False)
    assert r.returncode == 0, r.stderr
