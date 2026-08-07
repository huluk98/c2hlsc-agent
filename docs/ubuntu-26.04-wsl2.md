# Ubuntu 26.04 on WSL2

This checkout is intended to live in Ubuntu's native filesystem, such as
`~/c2hlsc-agent`, rather than under `/mnt/c` or another mounted Windows drive.
Keeping the repository and virtual environment on the Linux filesystem avoids
cross-platform executables, permissions, and filesystem-performance problems.

## Bootstrap

From the repository root:

```bash
bash scripts/setup_ubuntu_26_04.sh
source .venv/bin/activate
```

The bootstrap installs Ubuntu's Python and C/C++ build tools, creates `.venv`,
installs the package in editable mode, runs the unit tests, and performs an
offline vector-add conversion. It rejects required tools that resolve through
`/mnt/*`, so Windows executables are not silently used.

After the first successful run, skip the package-manager step when rebuilding
the environment:

```bash
C2HLSC_SKIP_APT=1 bash scripts/setup_ubuntu_26_04.sh
```

## Daily use

```bash
cd ~/c2hlsc-agent
source .venv/bin/activate
c2hlsc-agent --help
```

Run the test suite with:

```bash
python -m unittest discover -s tests
```

Run the offline example with:

```bash
python -m c2hlsc_agent.cli convert \
  --config examples/vector_add/config.yaml \
  --out build/vector_add \
  --no-run-vitis
```

## Vitis HLS

The Python agent and offline equivalence workflow are Linux-native after the
bootstrap. Full CSim, synthesis, and CoSim additionally require a Linux build
of AMD/Xilinx Vitis HLS and its license. A Windows Vitis installation visible
under `/mnt/*` does not satisfy that requirement and must not be added to this
environment's `PATH`.

With Linux Vitis installed, source its settings and verify the resolved binary:

```bash
source /opt/Xilinx/Vitis_HLS/2024.2/settings64.sh
command -v vitis_hls
```

The result should be a Linux path such as `/opt/Xilinx/...`, never `/mnt/*`.
