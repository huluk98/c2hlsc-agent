#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

usage() {
  cat >&2 <<'USAGE'
Usage:
  REMOTE=user@linux-host REMOTE_DIR=/absolute/or/home/path/c2hlsc_agent \
    scripts/run_remote_vitis.sh --config examples/vector_add/config.yaml --out build/vector_add

Required environment:
  REMOTE                SSH target, for example user@192.168.1.50

Optional environment:
  REMOTE_DIR            Remote repo directory, default ~/c2hlsc_agent
  C2HLSC_CONDA_ENV      Conda environment name, default c2hlsc-linux
  VITIS_SETTINGS_REMOTE Remote settings64.sh path. If unset, the remote runner probes common paths.

This script syncs the local package to the remote host, creates/updates the Conda
environment, then runs scripts/run_vitis_linux.sh on the remote host.
USAGE
}

if [[ $# -eq 0 || "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 2
fi

if [[ -z "${REMOTE:-}" ]]; then
  echo "REMOTE is required, for example REMOTE=user@linux-host." >&2
  usage
  exit 2
fi

REMOTE_DIR="${REMOTE_DIR:-~/c2hlsc_agent}"
ENV_NAME="${C2HLSC_CONDA_ENV:-c2hlsc-linux}"

if ! command -v rsync >/dev/null 2>&1; then
  echo "rsync not found locally." >&2
  exit 2
fi

if ! command -v ssh >/dev/null 2>&1; then
  echo "ssh not found locally." >&2
  exit 2
fi

ssh "${REMOTE}" "mkdir -p ${REMOTE_DIR}"
rsync -az \
  --exclude '.git' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude 'build/' \
  --exclude 'dist/' \
  "${REPO_ROOT}/" "${REMOTE}:${REMOTE_DIR}/"

remote_args=""
for arg in "$@"; do
  remote_args+=" $(printf '%q' "${arg}")"
done

remote_prefix="C2HLSC_CONDA_ENV=$(printf '%q' "${ENV_NAME}")"
if [[ -n "${VITIS_SETTINGS_REMOTE:-}" ]]; then
  remote_prefix+=" VITIS_SETTINGS=$(printf '%q' "${VITIS_SETTINGS_REMOTE}")"
fi

ssh "${REMOTE}" "cd ${REMOTE_DIR} && bash scripts/setup_linux_conda.sh && ${remote_prefix} bash scripts/run_vitis_linux.sh${remote_args}"
