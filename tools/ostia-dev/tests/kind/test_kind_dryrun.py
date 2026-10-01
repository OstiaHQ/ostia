"""Server-side admission of every golden manifest (R12): PSA restricted and the schema."""

from pathlib import Path

import pytest
import yaml
from kindlib import kubectl

pytestmark = pytest.mark.kind
GOLDEN = Path(__file__).resolve().parents[1] / "golden" / "manifests"
DRYRUN = "ostia-dryrun"
# rdma pods run as root with IPC_LOCK, which only a privileged namespace admits (§4.12)
DRYRUN_PRIVILEGED = "ostia-dryrun-priv"


@pytest.fixture(scope="module", autouse=True)
def namespace():
    for ns, level in ((DRYRUN, "restricted"), (DRYRUN_PRIVILEGED, "privileged")):
        kubectl("create", "namespace", ns, namespace=None, check=False)
        kubectl(
            "label",
            "namespace",
            ns,
            f"pod-security.kubernetes.io/enforce={level}",
            "--overwrite",
            namespace=None,
        )
    yield
    for ns in (DRYRUN, DRYRUN_PRIVILEGED):
        kubectl("delete", "namespace", ns, "--wait=false", namespace=None, check=False)


@pytest.mark.parametrize("golden", sorted(p.name for p in GOLDEN.glob("*.yaml")))
def test_the_api_server_admits_the_golden(golden):
    ns = DRYRUN_PRIVILEGED if golden.startswith("job-rdma-") else DRYRUN
    docs = list(yaml.safe_load_all((GOLDEN / golden).read_text()))
    for doc in docs:
        if doc.get("kind") not in ("Namespace", "ClusterRole"):
            doc["metadata"]["namespace"] = ns
        if doc.get("kind") == "Namespace":
            doc["metadata"]["name"] = ns
    text = yaml.safe_dump_all(docs)
    r = kubectl("apply", "--dry-run=server", "-f", "-", input=text, namespace=ns, check=False)
    assert r.returncode == 0, r.stderr
