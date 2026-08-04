#!/usr/bin/env bash
# Run Bambu HLS (PandA) on this machine via an amd64 Docker container.
#
# Bambu ships as an x86_64 Linux AppImage; on an Apple Silicon Mac we run it
# inside a linux/amd64 container (Docker emulates x86 via Rosetta). Only Bambu
# runs in the container -- the C sources and generated RTL live in <workdir> on
# the host, bind-mounted at /work.
#
# Usage:   scripts/bambu.sh <workdir> <bambu args...>
# Example: scripts/bambu.sh /tmp/job spec.c --top-fname=vector_add \
#            --generate-tb=test.xml --simulate --simulator=ICARUS
#
# Env overrides:
#   C2HLSC_BAMBU_SQUASHFS  extracted AppImage root (default ~/tools/eda/bambu/squashfs-root)
#   C2HLSC_BAMBU_IMAGE     base container image     (default ubuntu:22.04)
#   C2HLSC_BAMBU_PLATFORM  docker platform          (default linux/amd64)
#   C2HLSC_BAMBU_DOCKER    docker executable        (default docker)
set -euo pipefail

if [ "$#" -lt 1 ]; then
  echo "usage: bambu.sh <workdir> <bambu args...>" >&2
  exit 2
fi
WORKDIR="$1"; shift

SQUASHFS="${C2HLSC_BAMBU_SQUASHFS:-$HOME/tools/eda/bambu/squashfs-root}"
PLATFORM="${C2HLSC_BAMBU_PLATFORM:-linux/amd64}"
DOCKER="${C2HLSC_BAMBU_DOCKER:-docker}"

# Prefer the baked image (binutils/xz/make preinstalled -- Bambu needs them to
# unpack its bundled frontend); fall back to stock ubuntu if it was not built.
IMAGE="${C2HLSC_BAMBU_IMAGE:-}"
if [ -z "$IMAGE" ]; then
  if "$DOCKER" image inspect c2hlsc-bambu:local >/dev/null 2>&1; then
    IMAGE="c2hlsc-bambu:local"
  else
    IMAGE="ubuntu:22.04"
  fi
fi

if [ ! -x "$SQUASHFS/usr/bin/bambu" ]; then
  echo "bambu not found at $SQUASHFS/usr/bin/bambu" >&2
  echo "extract the AppImage: (cd <dir> && ./bambu-latest.AppImage --appimage-extract)" >&2
  echo "or set C2HLSC_BAMBU_SQUASHFS to the squashfs-root directory." >&2
  exit 3
fi

# Set the env the AppImage's launcher would normally set, then exec bambu.
# $0=bambu so "$@" inside the inner shell is exactly the bambu argument list.
exec "$DOCKER" run --rm --platform "$PLATFORM" \
  -v "$SQUASHFS":/opt/bambu:ro \
  -v "$WORKDIR":/work -w /work \
  "$IMAGE" bash -c '
    export APPDIR=/opt/bambu
    export PATH="$APPDIR/usr/bin:$PATH"
    export LD_LIBRARY_PATH="$APPDIR/usr/lib:$APPDIR/usr/lib/x86_64-linux-gnu:$APPDIR/lib/x86_64-linux-gnu:$APPDIR/usr/lib64"
    unset PYTHONHOME PYTHONPATH
    exec bambu "$@"' bambu "$@"
