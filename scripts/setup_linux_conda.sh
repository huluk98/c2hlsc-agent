#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${REPO_ROOT}/envs/environment-linux.yml"
ENV_NAME="${C2HLSC_CONDA_ENV:-c2hlsc-linux}"

if ! command -v conda >/dev/null 2>&1; then
  echo "conda not found. Install Miniconda/Mambaforge first, then rerun this script." >&2
  exit 2
fi

if conda env list | awk '{print $1}' | grep -qx "${ENV_NAME}"; then
  conda env update -n "${ENV_NAME}" -f "${ENV_FILE}" --prune
else
  conda env create -n "${ENV_NAME}" -f "${ENV_FILE}"
fi

conda run -n "${ENV_NAME}" python -m pip install -e "${REPO_ROOT}"
conda run -n "${ENV_NAME}" python -m unittest discover -s "${REPO_ROOT}/tests"

echo "Conda environment '${ENV_NAME}' is ready."
echo "Activate with: conda activate ${ENV_NAME}"
