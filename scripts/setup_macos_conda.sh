#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${REPO_ROOT}/envs/environment-macos.yml"
ENV_NAME="${C2HLSC_CONDA_ENV:-c2hlsc-agent}"
ENV_PREFIX="${C2HLSC_CONDA_PREFIX:-}"

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

mkdir -p "${CONDA_ENVS_PATH}" "${CONDA_PKGS_DIRS}" "${PIP_CACHE_DIR}"

if [[ -n "${ENV_PREFIX}" ]]; then
  if [[ -x "${ENV_PREFIX}/bin/python" ]]; then
    "${CONDA_BIN}" env update -p "${ENV_PREFIX}" -f "${ENV_FILE}" --prune
  else
    "${CONDA_BIN}" env create -p "${ENV_PREFIX}" -f "${ENV_FILE}"
  fi
  CONDA_RUN_ARGS=(-p "${ENV_PREFIX}")
  ACTIVATE_TARGET="${ENV_PREFIX}"
else
  if "${CONDA_BIN}" env list | awk '{print $1}' | grep -qx "${ENV_NAME}"; then
    "${CONDA_BIN}" env update -n "${ENV_NAME}" -f "${ENV_FILE}" --prune
  else
    "${CONDA_BIN}" env create -n "${ENV_NAME}" -f "${ENV_FILE}"
  fi
  CONDA_RUN_ARGS=(-n "${ENV_NAME}")
  ACTIVATE_TARGET="${ENV_NAME}"
fi

if [[ "${C2HLSC_INSTALL_LLM:-0}" == "1" ]]; then
  "${CONDA_BIN}" run "${CONDA_RUN_ARGS[@]}" python -m pip install -e "${REPO_ROOT}[yaml,templates,llm]"
else
  "${CONDA_BIN}" run "${CONDA_RUN_ARGS[@]}" python -m pip install -e "${REPO_ROOT}[yaml,templates]"
fi

"${CONDA_BIN}" run "${CONDA_RUN_ARGS[@]}" python -m unittest discover -s "${REPO_ROOT}/tests"

cat <<EOF

macOS Conda environment is ready:
  ${ACTIVATE_TARGET}

Activate it with:
  conda activate "${ACTIVATE_TARGET}"

Run local preflight with:
  bash scripts/preflight_local.sh

EOF
