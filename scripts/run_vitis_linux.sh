#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_NAME="${C2HLSC_CONDA_ENV:-c2hlsc-linux}"

usage() {
  cat >&2 <<'USAGE'
Usage:
  scripts/run_vitis_linux.sh --config examples/vector_add/config.yaml --out build/vector_add

Environment:
  C2HLSC_CONDA_ENV   Conda environment name, default c2hlsc-linux
  VITIS_SETTINGS     Path to Xilinx/Vitis settings64.sh. If unset, common paths are probed.

Examples:
  VITIS_SETTINGS=/opt/Xilinx/Vitis/2022.1/settings64.sh \
    scripts/run_vitis_linux.sh --config examples/vector_add/config.yaml --out build/vector_add

  scripts/run_vitis_linux.sh --input examples/bit_ops/input.c --top bit_ops --out build/bit_ops
USAGE
}

if [[ $# -eq 0 ]]; then
  usage
  exit 2
fi

if ! command -v conda >/dev/null 2>&1; then
  echo "conda not found. Run scripts/setup_linux_conda.sh after installing Miniconda/Mambaforge." >&2
  exit 2
fi

if [[ -z "${VITIS_SETTINGS:-}" ]]; then
  for candidate in \
    /tools/Xilinx/Vitis/2024.2/settings64.sh \
    /tools/Xilinx/Vitis/2023.2/settings64.sh \
    /tools/Xilinx/Vitis/2022.1/settings64.sh \
    /opt/Xilinx/Vitis/2024.2/settings64.sh \
    /opt/Xilinx/Vitis/2023.2/settings64.sh \
    /opt/Xilinx/Vitis/2022.1/settings64.sh; do
    if [[ -f "${candidate}" ]]; then
      VITIS_SETTINGS="${candidate}"
      break
    fi
  done
fi

if [[ -z "${VITIS_SETTINGS:-}" || ! -f "${VITIS_SETTINGS}" ]]; then
  echo "Vitis settings64.sh not found. Set VITIS_SETTINGS=/path/to/Vitis/<version>/settings64.sh." >&2
  exit 2
fi

# shellcheck disable=SC1090
source "${VITIS_SETTINGS}"

if ! command -v vitis_hls >/dev/null 2>&1; then
  echo "vitis_hls is still not on PATH after sourcing ${VITIS_SETTINGS}." >&2
  exit 2
fi

cd "${REPO_ROOT}"
conda run -n "${ENV_NAME}" python -m c2hlsc_agent.cli convert --run-vitis "$@"
