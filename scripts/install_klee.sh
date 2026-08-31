#!/usr/bin/env bash
# Build and install KLEE from source on Debian/Ubuntu.
#
# KLEE is packaged on Debian but NOT on Ubuntu (where the only `klee` match is a font)
# and not on macOS. This is the recipe that works on Ubuntu 24.04; it is the one the
# agent's own container uses. On macOS, do not use this script -- the generated
# tb/run_klee.py falls back to the official klee/klee container automatically once
# Docker Desktop is running (brew install --cask docker).
#
#   sudo bash scripts/install_klee.sh              # LLVM 16, /usr/local
#   LLVM=17 PREFIX=/opt/klee bash scripts/install_klee.sh
#
# Verify afterwards with: c2hlsc-agent doctor --tier symbolic
set -euo pipefail

LLVM="${LLVM:-16}"
PREFIX="${PREFIX:-/usr/local}"
WORKDIR="${WORKDIR:-$(mktemp -d)}"
JOBS="${JOBS:-$(nproc 2>/dev/null || echo 4)}"

echo "==> KLEE from source: LLVM ${LLVM}, prefix ${PREFIX}, workdir ${WORKDIR}"

if [ "$(id -u)" -ne 0 ] && [ ! -w "${PREFIX}" ]; then
  echo "error: ${PREFIX} is not writable; re-run with sudo or set PREFIX" >&2
  exit 1
fi

echo "==> installing build dependencies"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# KLEE requires LLVM 15 or newer; gperftools and z3 are hard requirements of the
# default configuration, and cmake's checks fail confusingly without them.
apt-get install -y --no-install-recommends \
  "llvm-${LLVM}-dev" "llvm-${LLVM}-tools" "clang-${LLVM}" "libclang-${LLVM}-dev" \
  z3 libz3-dev libgoogle-perftools-dev libsqlite3-dev \
  cmake ninja-build build-essential git python3-tabulate

echo "==> cloning KLEE"
rm -rf "${WORKDIR}/klee_src"
git clone --depth 1 https://github.com/klee/klee.git "${WORKDIR}/klee_src"

echo "==> configuring"
# The POSIX runtime, uclibc and libcxx are only needed to symbolically execute programs
# that use the C library or the STL. The generated driver calls one self-contained top
# function, so leaving them off cuts the build substantially without losing anything here.
cmake -S "${WORKDIR}/klee_src" -B "${WORKDIR}/klee_build" -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DLLVM_CONFIG_BINARY="/usr/bin/llvm-config-${LLVM}" \
  -DLLVMCC="/usr/bin/clang-${LLVM}" \
  -DLLVMCXX="/usr/bin/clang++-${LLVM}" \
  -DENABLE_SOLVER_Z3=ON \
  -DENABLE_POSIX_RUNTIME=OFF \
  -DENABLE_KLEE_UCLIBC=OFF \
  -DENABLE_KLEE_LIBCXX=OFF \
  -DENABLE_UNIT_TESTS=OFF \
  -DENABLE_SYSTEM_TESTS=OFF \
  -DCMAKE_INSTALL_PREFIX="${PREFIX}"

echo "==> building (${JOBS} jobs)"
ninja -C "${WORKDIR}/klee_build" -j "${JOBS}"

echo "==> installing to ${PREFIX}"
ninja -C "${WORKDIR}/klee_build" install

echo "==> verifying"
"${PREFIX}/bin/klee" --version | head -3
test -f "${PREFIX}/include/klee/klee.h" || {
  echo "error: klee/klee.h was not installed under ${PREFIX}/include" >&2
  exit 1
}
echo "==> done. klee: ${PREFIX}/bin/klee, headers: ${PREFIX}/include/klee/klee.h"
echo "    Check the agent sees it:  c2hlsc-agent doctor --tier symbolic"
