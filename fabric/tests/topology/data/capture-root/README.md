# Fake sysfs root for topology capture tests

A static, committed `/sys` subset (real symlinks) read through `SysfsReader` in the
`ostia_topo_capture_tests` unit tests and by the capture tool's `--sysfs-root`. Every identifier
below is planted so the scrub and leak checks have something to find. Addresses come from
documentation ranges (192.0.2.0/24, 2001:db8::/32) or the locally administered `02:00:5e:...` prefix.

## PCI devices

| Bus ID | Class | Vendor:device | Driver | Notes |
| --- | --- | --- | --- | --- |
| `0000:11:00.0` | `0x030200` | `10de:27b8` (NVIDIA L4) | nvidia | `max_link_speed` 16.0 GT/s PCIe, width 16, NUMA 0 |
| `0000:11:01.0` | `0x020000` | `15b3:101b` (ConnectX-6) | mlx5_core | ethernet NIC, `net/ens5`, 100000 Mb/s |
| `0000:12:00.0` | `0x020700` | `15b3:1021` (ConnectX-7) | mlx5_core | InfiniBand NIC `mlx5_1`, 400 Gb/sec (4X NDR), no `net/` (hidden network namespace) |
| `0000:12:00.2` | `0x020700` | `15b3:101e` | mlx5_core | SR-IOV VF of `0000:12:00.0` (`physfn` link), skipped by the NIC scan |
| `0000:13:00.0` | `0x010802` | `144d:a80a` (NVMe) | none | ignored by the NIC scan |

## Planted values

- `0000:11:01.0/net/ens5/address`: `02:00:5e:10:00:01`
- `0000:12:00.0/infiniband/mlx5_1/node_guid` and `sys_image_guid`: `0200:5eff:fe20:0001`
- `.../ports/1/gids/0`: `2001:0db8:0000:0000:0200:5eff:fe20:0001`; `gids/1` is all zeros
- `class/dmi/id/product_name`: `g6.4xlarge`; `sys_vendor`: `Amazon EC2`
- `class/dmi/id/board_asset_tag`: `i-0123456789abcdef0`
- `class/dmi/id/product_serial`: `ec2-serial-planted`
- `class/net/ens5` and `class/infiniband/mlx5_1` are symlinks to the devices above

## Invariant

`hwloc-input.xml` (next to this directory) holds exactly the PCI devices in the table above,
with the same bus IDs, classes and vendor/device IDs.
It also plants identifier-bearing hwloc infos (`HostName`, `DMI*`, `OSRelease`, `NVIDIAUUID`,
`Address`, `PCISlot`, `SerialNumber`) and OS devices (a GPU, a MAC-derived `enx...` netdev) that
the hwloc.xml emitter must drop (RFC-0003 §2.1). Keep the two in step when adding a device.
