#!/bin/sh
# Install the pinned CMake 4.1 and Ninja releases into PREFIX, verifying their SHA-256.
# Used by the container jobs that build without pixi (RFC-0001 §1.4, tier 2).
#   tools/ci/install_cmake.sh /opt/ostia-tools && export PATH=/opt/ostia-tools/bin:$PATH
set -eu
PREFIX=${1:-/opt/ostia-tools}
CMAKE_VERSION=4.1.6
NINJA_VERSION=1.13.2
case "$(uname -m)" in
  x86_64)
    CMAKE_ARCH=x86_64
    CMAKE_SHA=d5c2e72820e01f1c3a07092d0a29e209263a7d22f55b4ad7f414ee870ae6b8e0
    NINJA_ZIP=ninja-linux.zip
    NINJA_SHA=5749cbc4e668273514150a80e387a957f933c6ed3f5f11e03fb30955e2bbead6 ;;
  aarch64)
    CMAKE_ARCH=aarch64
    CMAKE_SHA=8b3e3af8e4b4e95224a4490f4772adb7a512be34c8380fe76e7be003e0fd4394
    NINJA_ZIP=ninja-linux-aarch64.zip
    NINJA_SHA=fd2cacc8050a7f12a16a2e48f9e06fca5c14fc4c2bee2babb67b58be17a607fc ;;
  *) echo "error: unsupported architecture $(uname -m)" >&2; exit 1 ;;
esac
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
tarball=cmake-$CMAKE_VERSION-linux-$CMAKE_ARCH.tar.gz
curl -fsSL -o "$tmp/$tarball" "https://github.com/Kitware/CMake/releases/download/v$CMAKE_VERSION/$tarball"
echo "$CMAKE_SHA  $tmp/$tarball" | sha256sum -c -
curl -fsSL -o "$tmp/$NINJA_ZIP" "https://github.com/ninja-build/ninja/releases/download/v$NINJA_VERSION/$NINJA_ZIP"
echo "$NINJA_SHA  $tmp/$NINJA_ZIP" | sha256sum -c -
mkdir -p "$PREFIX/bin"
tar -xzf "$tmp/$tarball" -C "$PREFIX" --strip-components=1
python3 -c "import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])" "$tmp/$NINJA_ZIP" "$PREFIX/bin"
chmod +x "$PREFIX/bin/ninja"
"$PREFIX/bin/cmake" --version | head -1
"$PREFIX/bin/ninja" --version
