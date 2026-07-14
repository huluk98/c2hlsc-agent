# Minimal amd64 runtime for the Bambu HLS AppImage.
# The AppImage bundles its own clang/gcc frontends but shells out to a few base
# tools at runtime (ar/xz to unpack its frontend, make, etc.) that the stock
# ubuntu:22.04 image lacks. Bake them once so each `bambu.sh` run is fast.
#
# Build (from repo root):
#   docker build --platform linux/amd64 -f scripts/bambu.Dockerfile -t c2hlsc-bambu:local .
# build-essential: Bambu's bundled clang compiles the C spec against the system
# libc dev headers (bits/libc-header-start.h etc.) and Verilator builds its model
# with g++ -- both need the full C/C++ toolchain, not just binutils.
# verilator: this Bambu build co-simulates with Verilator (not Icarus).
# gcc-multilib/g++-multilib: Bambu's I386_* frontends compile the C spec as 32-bit,
# which needs the i386 libc dev headers (gnu/stubs-32.h, libc6-dev-i386).
FROM ubuntu:22.04
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential gcc-multilib g++-multilib \
      xz-utils ca-certificates zlib1g libtinfo5 \
      verilator perl \
 && rm -rf /var/lib/apt/lists/*
