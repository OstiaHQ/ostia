"""ostia-dev coverage's C++ report: which binaries carry the coverage mapping."""

from ostia_dev.dev.coverage import binaries


def _touch(path, executable=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    path.chmod(0o755 if executable else 0o644)
    return path


def test_binaries_are_ostia_libraries_and_programs_outside_deps(tmp_path):
    lib = _touch(tmp_path / "fabric/libostia-fabric.so")
    tests = _touch(tmp_path / "fabric/tests/ostia_fabric_tests", executable=True)
    tool = _touch(tmp_path / "fabric/tools/topo/ostia-topo", executable=True)
    (tmp_path / "fabric/libostia-fabric.so.0").symlink_to(lib)
    _touch(tmp_path / "_deps/googletest-build/ostia_lookalike", executable=True)
    _touch(tmp_path / "fabric/CMakeFiles/ostia_obj", executable=True)
    _touch(tmp_path / "fabric/ostia-summary.txt")
    _touch(tmp_path / "tests/gtest_main", executable=True)
    assert binaries(tmp_path) == sorted([lib, tests, tool])
