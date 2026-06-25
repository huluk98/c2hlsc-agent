#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_NAME="${C2HLSC_CONDA_ENV:-c2hlsc-linux}"
USE_ACTIVE_ENV="${C2HLSC_USE_ACTIVE_ENV:-0}"

usage() {
  cat >&2 <<'USAGE'
Usage:
  scripts/run_vitis_linux.sh --config examples/vector_add/config.yaml --out build/vector_add

Environment:
  C2HLSC_CONDA_ENV   Conda environment name, default c2hlsc-linux
  C2HLSC_USE_ACTIVE_ENV
                     Set to 1 to use the currently active shell Python instead
                     of conda run -n <env>. Use this when your terminal already
                     has the desired env activated, e.g. conda activate hlsc.
  VITIS_SETTINGS     Path to Xilinx/Vitis settings64.sh or .settings64-Vitis_HLS.sh.
  VITIS_HLS_ROOT     Path to the Vitis_HLS/<version> directory, for example
                     "/path/to/Vitis_HLS/2024.2".
  VITIS_HLS_BIN      Direct path to the vitis_hls executable. This is the most explicit fallback.

Examples:
  VITIS_HLS_ROOT="/path/to/Vitis_HLS/2024.2" \
    scripts/run_vitis_linux.sh --config examples/vector_add/config.yaml --out build/vector_add

  C2HLSC_USE_ACTIVE_ENV=1 \
  VITIS_HLS_ROOT="/path/to/Vitis_HLS/2024.2" \
    scripts/run_vitis_linux.sh --config examples/vector_add/config.yaml --out build/vector_add

  VITIS_SETTINGS="/opt/Xilinx/Vitis/2024.2/settings64.sh" \
    scripts/run_vitis_linux.sh --config examples/vector_add/config.yaml --out build/vector_add

  scripts/run_vitis_linux.sh --input examples/bit_ops/input.c --top bit_ops --out build/bit_ops
USAGE
}

if [[ $# -eq 0 ]]; then
  usage
  exit 2
fi

if [[ "${USE_ACTIVE_ENV}" != "1" && "${USE_ACTIVE_ENV}" != "true" ]]; then
  if ! command -v conda >/dev/null 2>&1; then
    echo "conda not found. Run scripts/setup_linux_conda.sh after installing Miniconda/Mambaforge." >&2
    exit 2
  fi
fi

if [[ -n "${VITIS_HLS_BIN:-}" ]]; then
  if [[ ! -x "${VITIS_HLS_BIN}" ]]; then
    echo "VITIS_HLS_BIN is set but not executable: ${VITIS_HLS_BIN}" >&2
    exit 2
  fi
  export PATH="$(dirname "${VITIS_HLS_BIN}"):${PATH}"
fi

if [[ -n "${VITIS_HLS_ROOT:-}" ]]; then
  if [[ ! -d "${VITIS_HLS_ROOT}" ]]; then
    echo "VITIS_HLS_ROOT is set but does not exist: ${VITIS_HLS_ROOT}" >&2
    exit 2
  fi
  export PATH="${VITIS_HLS_ROOT}/bin:${PATH}"
  if [[ -z "${VITIS_SETTINGS:-}" ]]; then
    for candidate in \
      "${VITIS_HLS_ROOT}/settings64.sh" \
      "${VITIS_HLS_ROOT}/.settings64-Vitis_HLS.sh"; do
      if [[ -f "${candidate}" ]]; then
        VITIS_SETTINGS="${candidate}"
        break
      fi
    done
  fi
fi

if [[ -z "${VITIS_SETTINGS:-}" ]]; then
  for candidate in \
    /tools/Xilinx/Vitis/2024.2/settings64.sh \
    /tools/Xilinx/Vitis/2023.2/settings64.sh \
    /tools/Xilinx/Vitis/2022.1/settings64.sh \
    /tools/Xilinx/Vitis_HLS/2024.2/.settings64-Vitis_HLS.sh \
    /tools/Xilinx/Vitis_HLS/2023.2/.settings64-Vitis_HLS.sh \
    /tools/Xilinx/Vitis_HLS/2022.1/.settings64-Vitis_HLS.sh \
    /opt/Xilinx/Vitis/2024.2/settings64.sh \
    /opt/Xilinx/Vitis/2023.2/settings64.sh \
    /opt/Xilinx/Vitis/2022.1/settings64.sh \
    /opt/Xilinx/Vitis_HLS/2024.2/.settings64-Vitis_HLS.sh \
    /opt/Xilinx/Vitis_HLS/2023.2/.settings64-Vitis_HLS.sh \
    /opt/Xilinx/Vitis_HLS/2022.1/.settings64-Vitis_HLS.sh; do
    if [[ -f "${candidate}" ]]; then
      VITIS_SETTINGS="${candidate}"
      break
    fi
  done
fi

if [[ -n "${VITIS_SETTINGS:-}" ]]; then
  if [[ ! -f "${VITIS_SETTINGS}" ]]; then
    echo "VITIS_SETTINGS is set but does not exist: ${VITIS_SETTINGS}" >&2
    exit 2
  fi
  # shellcheck disable=SC1090
  source "${VITIS_SETTINGS}"
fi

if ! command -v vitis_hls >/dev/null 2>&1; then
  echo "vitis_hls is not on PATH." >&2
  echo "Set one of these and rerun:" >&2
  echo "  VITIS_HLS_ROOT=/path/to/Vitis_HLS/2024.2" >&2
  echo "  VITIS_HLS_BIN=/path/to/Vitis_HLS/2024.2/bin/vitis_hls" >&2
  echo "  VITIS_SETTINGS=/path/to/settings64.sh" >&2
  exit 2
fi

echo "Using vitis_hls: $(command -v vitis_hls)"

cd "${REPO_ROOT}"
if [[ "${USE_ACTIVE_ENV}" == "1" || "${USE_ACTIVE_ENV}" == "true" ]]; then
  if ! command -v python >/dev/null 2>&1; then
    echo "python not found in the active shell environment." >&2
    exit 2
  fi
  echo "Using active Python: $(command -v python)"
  python -m c2hlsc_agent.cli convert --run-vitis "$@"
else
  echo "Using Conda env: ${ENV_NAME}"
  conda run -n "${ENV_NAME}" python -m c2hlsc_agent.cli convert --run-vitis "$@"
fi
