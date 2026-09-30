"""External commands and the -v echo (RFC-0005 §1.3)."""

import sys

import pytest

from ostia_dev import proc


@pytest.fixture(autouse=True)
def _quiet():
    proc.set_verbose(False)
    yield
    proc.set_verbose(False)


def test_run_captures_output_and_code():
    r = proc.run([sys.executable, "-c", "import sys; print('hi'); sys.exit(3)"])
    assert r.returncode == 3 and r.stdout.strip() == "hi"


def test_run_passes_input():
    r = proc.run([sys.executable, "-c", "import sys; print(sys.stdin.read()[::-1])"], input="abc")
    assert r.stdout.strip() == "cba"


def test_run_check_raises():
    with pytest.raises(proc.CalledProcessError):
        proc.run([sys.executable, "-c", "raise SystemExit(1)"], check=True)


def test_echo_is_off_by_default(capsys):
    proc.run([sys.executable, "-c", "pass"])
    assert capsys.readouterr().err == ""


def test_verbose_echoes_the_command(capsys):
    proc.set_verbose(True)
    proc.run([sys.executable, "-c", "print('a b')"])
    assert capsys.readouterr().err == f"+ {sys.executable} -c 'print('\"'\"'a b'\"'\"')'\n"


def test_verbose_argument_overrides_the_global(capsys):
    proc.run(["true"], verbose=True)
    assert capsys.readouterr().err == "+ true\n"


def test_stream_yields_lines(capsys):
    p = proc.stream([sys.executable, "-c", "print('one'); print('two')"], verbose=True)
    assert [line.rstrip("\n") for line in p.stdout] == ["one", "two"]
    assert p.wait() == 0
    assert capsys.readouterr().err.startswith("+ ")
