#!/bin/sh
# GPU preflight for remote runs (RFC-0005 §4.9): print the machine's
# fingerprint and fail on a driver older than the CUDA 13 floor (R580).
set -eu
MIN_DRIVER=580
echo "## GPU fingerprint"
nvidia-smi --query-gpu=name,driver_version,compute_cap,memory.total --format=csv
uname -srm
driver=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1)
major=${driver%%.*}
if [ "$major" -lt "$MIN_DRIVER" ]; then
  echo "error: GPU driver $driver is older than R$MIN_DRIVER"
  echo "  rule: CUDA 13 needs driver R580 or newer (RFC-0001 §1.1)"
  echo "  fix: run on a node with driver R580 or newer (on GKE, set the node pool's gpu-driver-version=latest)"
  echo "  see: RFC-0005 §4.9"
  exit 1
fi
echo "driver $driver: ok"
