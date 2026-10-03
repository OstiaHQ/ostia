"""Tests for ostia_dev/ci/check_fixture_leaks.py (RFC-0003 §3, CI pass)."""

import pytest
from ostia_dev.ci.check_fixture_leaks import check_text, main

PLANTED = [
    ('"addr": "10.128.0.17"', "ipv4"),
    ('"addr": "fe80::1c2b:3cff:fe4d:5e6f"', "ipv6"),
    ('"addr": "2600:1f18:abcd::12"', "ipv6"),
    ('"addr": "2600:1f18:abcd:0:1c2b:3cff:fe4d:5e6f"', "ipv6"),
    ('"mac": "0c:42:a1:5e:6f:70"', "mac"),
    ('"mac": "0C-42-A1-5E-6F-70"', "mac"),
    ('"uuid": "GPU-6b9f5664-1234-5678-9abc-def012345678"', "gpu-uuid"),
    ('"mig": "MIG-6b9f5664-1234-5678-9abc-def012345678"', "mig-uuid"),
    ('"id": "i-0a1b2c3d4e5f67890"', "instance-id"),
    ('"host": "ip-10-0-1-23"', "ec2-hostname"),
    ('<info name="SerialNumber" value="1323020034567"/>', "serial"),
    ('"serial": "1652520012345"', "serial"),
    ('<info name="HostName" value="x"/>', "hwloc-key"),
    ('<info name="DMIProductUUID" value="x"/>', "hwloc-key"),
    ('<info value="x" name="HostName"/>', "hwloc-key"),
    ("<info name='HostName' value=\"x\"/>", "hwloc-key"),
    ('<info name = "HostName" value="x"/>', "hwloc-key"),
    ('"0000:00:02.0 aa:bb:cc:dd:ee:ff"', "mac"),
]

LEGITIMATE = [
    '"bus_id": "0000:07:00.0"',
    'pci_busid="0000:3b:00.1" pci_type="0207 [15b3:101b] [15b3:0007] 00"',
    '"key": "switch-group-0"',
    '"key": "hostbridge-0000:00"',
    'bridge_pci="0000:[00-ff]"',
    '"topology_id": "topo1:sha256:'
    '5d1e8a7f00112233445566778899aabbccddeeff00112233445566778899aabb"',
    'cpuset="0x000000ff,0xffffffff"',
    '"driver_version": "580.173.01"',
    '"captured_at": "2026-10-03T12:00:00Z"',
    'pci_link_speed="31.507692"',
    '"time": "12:00:00"',
    '"addr": "999.1.1.1"',
    '<info name="OstiaPCIeMaxGen" value="4"/>',
    "<info value=\"4\" name='OstiaPCIeMaxGen'/>",
    '<info name="CPUModel" value="Fictional CPU"/>',
]


@pytest.mark.parametrize("line,kind", PLANTED)
def test_planted_identifier_is_found(line, kind):
    assert [f.kind for f in check_text(line + "\n", "f.json")] == [kind]


@pytest.mark.parametrize("line", LEGITIMATE)
def test_legitimate_fixture_content_passes(line):
    assert check_text(line + "\n", "f.json") == []


def test_output_names_line_and_kind_but_never_the_value(tmp_path, capsys):
    f = tmp_path / "nics.json"
    f.write_text('{\n  "mac": "0c:42:a1:5e:6f:70"\n}\n')
    assert main(["--files", str(f)]) == 1
    out = capsys.readouterr().out
    assert f"{f}:2: mac" in out
    assert "0c:42" not in out


def test_committed_fixtures_are_clean():
    assert main([]) == 0
