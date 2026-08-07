#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_DIR="${C2HLSC_VENV_DIR:-${REPO_ROOT}/.venv}"

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "This bootstrap must run on Linux." >&2
  exit 2
fi

case "${REPO_ROOT}" in
  /mnt/*)
    echo "Move the checkout into the Linux filesystem (for example, ~/c2hlsc-agent)." >&2
    exit 2
    ;;
esac

case "${VENV_DIR}" in
  /mnt/*)
    echo "Place the virtual environment in the Linux filesystem, not under /mnt." >&2
    exit 2
    ;;
esac

APT_PACKAGES=(
  python3-pip
  python3-venv
  build-essential
  cmake
  ninja-build
  rsync
  pkg-config
  git
  gh
)

if [[ "${C2HLSC_SKIP_APT:-0}" != "1" ]]; then
  if [[ "${EUID}" -eq 0 ]]; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y "${APT_PACKAGES[@]}"
  else
    sudo apt-get update
    sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y "${APT_PACKAGES[@]}"
  fi
fi

for tool in python3 gcc g++ make cmake git; do
  resolved="$(command -v "${tool}" || true)"
  if [[ -z "${resolved}" ]]; then
    echo "Required Linux tool is missing: ${tool}" >&2
    exit 2
  fi
  case "${resolved}" in
    /mnt/*)
      echo "Refusing Windows-interoperability tool for ${tool}: ${resolved}" >&2
      exit 2
      ;;
  esac
done

cd "${REPO_ROOT}"
python3 -m venv "${VENV_DIR}"
"${VENV_DIR}/bin/python" -m pip install --upgrade pip setuptools wheel
"${VENV_DIR}/bin/python" -m pip install -r requirements.txt
"${VENV_DIR}/bin/python" -m pip install -e .
"${VENV_DIR}/bin/python" -m unittest discover -s tests
"${VENV_DIR}/bin/python" -m c2hlsc_agent.cli convert \
  --config examples/vector_add/config.yaml \
  --out build/ubuntu-26.04-vector-add \
  --no-run-vitis

echo "Ubuntu setup passed."
echo "Activate with: source ${VENV_DIR}/bin/activate"
echo "Smoke output: ${REPO_ROOT}/build/ubuntu-26.04-vector-add"
