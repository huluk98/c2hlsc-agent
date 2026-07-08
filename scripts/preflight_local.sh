#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_NAME="${C2HLSC_CONDA_ENV:-c2hlsc-agent}"
ENV_PREFIX="${C2HLSC_CONDA_PREFIX:-}"
OUT_DIR="${C2HLSC_PREFLIGHT_OUT:-${REPO_ROOT}/build/preflight_vector_add}"

export CONDA_ENVS_PATH="${CONDA_ENVS_PATH:-${HOME}/.conda/envs}"
export CONDA_PKGS_DIRS="${CONDA_PKGS_DIRS:-${HOME}/.conda/pkgs}"
export PIP_CACHE_DIR="${PIP_CACHE_DIR:-${HOME}/.conda/pip-cache}"

resolve_conda() {
  if [[ -n "${CONDA_EXE:-}" && -x "${CONDA_EXE}" ]]; then
    printf '%s\n' "${CONDA_EXE}"
  elif command -v conda >/dev/null 2>&1; then
    command -v conda
  elif [[ -x /opt/homebrew/bin/conda ]]; then
    printf '%s\n' /opt/homebrew/bin/conda
  elif [[ -x /opt/homebrew/Caskroom/miniforge/base/bin/conda ]]; then
    printf '%s\n' /opt/homebrew/Caskroom/miniforge/base/bin/conda
  else
    return 1
  fi
}

CONDA_BIN="$(resolve_conda)" || {
  echo "conda not found. Install Miniforge first, then rerun this script." >&2
  exit 2
}

if [[ -n "${ENV_PREFIX}" ]]; then
  if [[ ! -x "${ENV_PREFIX}/bin/python" ]]; then
    echo "Environment is missing: ${ENV_PREFIX}" >&2
    echo "Run: bash scripts/setup_macos_conda.sh" >&2
    exit 2
  fi
  CONDA_RUN_ARGS=(-p "${ENV_PREFIX}")
else
  if ! "${CONDA_BIN}" env list | awk '{print $1}' | grep -qx "${ENV_NAME}"; then
    echo "Environment is missing: ${ENV_NAME}" >&2
    echo "Run: bash scripts/setup_macos_conda.sh" >&2
    exit 2
  fi
  CONDA_RUN_ARGS=(-n "${ENV_NAME}")
fi

cd "${REPO_ROOT}"

echo "== Python =="
"${CONDA_BIN}" run "${CONDA_RUN_ARGS[@]}" python --version

echo "== Package =="
"${CONDA_BIN}" run "${CONDA_RUN_ARGS[@]}" python -m pip install -e "${REPO_ROOT}[yaml,templates]"

echo "== Unit tests =="
"${CONDA_BIN}" run "${CONDA_RUN_ARGS[@]}" python -m unittest discover -s tests

echo "== Offline conversion smoke =="
rm -rf "${OUT_DIR}"
"${CONDA_BIN}" run "${CONDA_RUN_ARGS[@]}" python -m c2hlsc_agent.cli convert \
  --config examples/vector_add/config.yaml \
  --out "${OUT_DIR}" \
  --no-run-vitis

test -f "${OUT_DIR}/conversion_report.json"
test -f "${OUT_DIR}/src/hls_top.cpp"
test -f "${OUT_DIR}/tb/testbench.cpp"

echo "Preflight passed. Output: ${OUT_DIR}"
