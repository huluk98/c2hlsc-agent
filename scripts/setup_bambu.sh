#!/usr/bin/env bash
# One-time setup for the local (Vitis-free) co-simulation backend.
#
# Installs PandA Bambu as a containerized amd64 tool so `--cosim-backend local-hls`
# can synthesize C to Verilog and co-simulate it locally -- no Vitis, works on
# Apple Silicon. Idempotent: re-running skips steps already done.
#
#   scripts/setup_bambu.sh
#
# Env overrides: C2HLSC_BAMBU_DIR (default ~/tools/eda/bambu),
#                C2HLSC_BAMBU_APPIMAGE_URL (default the official 'latest').
set -euo pipefail

BAMBU_DIR="${C2HLSC_BAMBU_DIR:-$HOME/tools/eda/bambu}"
APPIMAGE_URL="${C2HLSC_BAMBU_APPIMAGE_URL:-https://release.bambuhls.eu/bambu-latest.AppImage}"
APPIMAGE="$BAMBU_DIR/bambu-latest.AppImage"
SQUASHFS="$BAMBU_DIR/squashfs-root"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

command -v docker >/dev/null || { echo "docker is required (Docker Desktop / colima)"; exit 1; }
docker info >/dev/null 2>&1 || { echo "docker daemon is not running"; exit 1; }
mkdir -p "$BAMBU_DIR"

if [ ! -f "$APPIMAGE" ]; then
  echo "==> downloading Bambu AppImage (~1.3 GiB) from $APPIMAGE_URL"
  curl -fL --retry 3 -o "$APPIMAGE" "$APPIMAGE_URL"
fi
chmod +x "$APPIMAGE"

if [ ! -x "$SQUASHFS/usr/bin/bambu" ]; then
  echo "==> extracting the AppImage (once) inside an amd64 container"
  docker run --rm --platform linux/amd64 -v "$BAMBU_DIR":/opt/bambu -w /opt/bambu \
    ubuntu:22.04 ./bambu-latest.AppImage --appimage-extract >/dev/null
fi

echo "==> building the c2hlsc-bambu:local runtime image (verilator + toolchain)"
docker build --platform linux/amd64 -f "$HERE/bambu.Dockerfile" -t c2hlsc-bambu:local "$HERE/.." >/dev/null

echo "==> smoke test: bambu --version"
docker run --rm --platform linux/amd64 -v "$SQUASHFS":/opt/bambu:ro c2hlsc-bambu:local bash -c '
  export APPDIR=/opt/bambu PATH="/opt/bambu/usr/bin:$PATH"
  export LD_LIBRARY_PATH="/opt/bambu/usr/lib:/opt/bambu/usr/lib/x86_64-linux-gnu:/opt/bambu/lib/x86_64-linux-gnu:/opt/bambu/usr/lib64"
  unset PYTHONHOME PYTHONPATH; bambu --version' | grep -i version || true

echo "==> done. Run the local ladder with:  --cosim-backend local-hls"
