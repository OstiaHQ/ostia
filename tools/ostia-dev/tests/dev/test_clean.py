"""`ostia-dev clean` removes what install-native put into the environment (RFC-0001 §3.5)."""

import subprocess
import sys

from ostia_dev.paths import ROOT


def test_clean_uses_the_native_install_manifest(tmp_path):
    installed = tmp_path / "prefix" / "lib" / "libostia-telemetry.so"
    installed.parent.mkdir(parents=True)
    installed.write_text("")
    build = tmp_path / "build" / "default"
    (build / "dev").mkdir(parents=True)
    # install-native's own record, which ctest's staging install cannot overwrite.
    (build / "native-install-manifest.txt").write_text(f"{installed}\n")
    # ctest's staging install rewrote the preset's manifest with other paths.
    (build / "dev" / "install_manifest.txt").write_text(f"{tmp_path}/stage/lib/x.so\n")
    r = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/ostia-dev/src/ostia_dev/dev/clean.py"),
            "--build-root",
            str(build),
            "--skip-pip",
        ],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert not installed.exists()
    assert not build.exists()
