#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python}"
CONFIG_PATH="${HLS_NL_COSIM_CONFIG:-configs/hls_nl_cosim_smoke.json}"

usage() {
  cat >&2 <<'USAGE'
Usage:
  VITIS_HLS_BIN="/path/to/Vitis_HLS/2024.2/bin/vitis_hls" \
    bash scripts/run_hls_nl_cosim_smoke.sh

Optional environment overrides:
  HLS_NL_COSIM_CONFIG  JSON config path, default configs/hls_nl_cosim_smoke.json
  HLS_NL_JSONL         Override config input JSONL path
  HLS_NL_OUT_DIR       Override config output directory
  HLS_NL_OFFSET        Override config offset
  HLS_NL_LIMIT         Override config limit
  VITIS_PART           Override config part
  VITIS_CLOCK          Override config clock period
  VITIS_HLS_BIN        Full path to vitis_hls
  VITIS_HLS_ROOT       Path to Vitis_HLS/<version>; adds bin to PATH
  VITIS_SETTINGS       Path to Vitis settings64.sh or .settings64-Vitis_HLS.sh
  HLS_NL_GENERATE_ONLY Set to 1 to generate projects without running Vitis
USAGE
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 2
fi

cd "${REPO_ROOT}"

if [[ -z "${HLS_NL_JSONL:-}" ]]; then
  CONFIG_HAS_INPUT="$("${PYTHON_BIN}" - "${CONFIG_PATH}" <<'PY'
import json
import sys
from pathlib import Path

config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print("1" if config.get("input") else "0")
PY
)"
  if [[ "${CONFIG_HAS_INPUT}" != "1" ]]; then
    echo "HLS_NL_JSONL is required because ${CONFIG_PATH} does not hard-code a dataset path." >&2
    echo "Example:" >&2
    echo "  HLS_NL_JSONL=/path/to/hls_nl_repaired.accepted.jsonl bash scripts/run_hls_nl_cosim_smoke.sh" >&2
    exit 2
  fi
fi

if [[ -n "${VITIS_HLS_ROOT:-}" ]]; then
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

if [[ -n "${VITIS_SETTINGS:-}" ]]; then
  if [[ ! -f "${VITIS_SETTINGS}" ]]; then
    echo "VITIS_SETTINGS is set but does not exist: ${VITIS_SETTINGS}" >&2
    exit 2
  fi
  # shellcheck disable=SC1090
  source "${VITIS_SETTINGS}"
fi

if [[ -n "${VITIS_HLS_BIN:-}" ]]; then
  if [[ ! -x "${VITIS_HLS_BIN}" ]]; then
    echo "VITIS_HLS_BIN is set but not executable: ${VITIS_HLS_BIN}" >&2
    exit 2
  fi
  export PATH="$(dirname "${VITIS_HLS_BIN}"):${PATH}"
fi

ARGS=(--config "${CONFIG_PATH}" --run-full-cosim)

if [[ -n "${HLS_NL_JSONL:-}" ]]; then
  ARGS+=(--input "${HLS_NL_JSONL}")
fi
if [[ -n "${HLS_NL_OUT_DIR:-}" ]]; then
  ARGS+=(--out-dir "${HLS_NL_OUT_DIR}")
fi
if [[ -n "${HLS_NL_OFFSET:-}" ]]; then
  ARGS+=(--offset "${HLS_NL_OFFSET}")
fi
if [[ -n "${HLS_NL_LIMIT:-}" ]]; then
  ARGS+=(--limit "${HLS_NL_LIMIT}")
fi
if [[ -n "${VITIS_PART:-}" ]]; then
  ARGS+=(--part "${VITIS_PART}")
fi
if [[ -n "${VITIS_CLOCK:-}" ]]; then
  ARGS+=(--clock "${VITIS_CLOCK}")
fi
if [[ "${HLS_NL_GENERATE_ONLY:-0}" == "1" || "${HLS_NL_GENERATE_ONLY:-0}" == "true" ]]; then
  ARGS+=(--generate-only)
fi

echo "Running HLS_NL CoSim smoke:"
printf '  %q' "${PYTHON_BIN}" scripts/run_hls_nl_vitis_batch.py "${ARGS[@]}"
printf '\n'

"${PYTHON_BIN}" scripts/run_hls_nl_vitis_batch.py "${ARGS[@]}"

OUT_DIR="${HLS_NL_OUT_DIR:-$("${PYTHON_BIN}" - "${CONFIG_PATH}" <<'PY'
import json
import sys
from pathlib import Path

config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(config.get("out_dir", "build/hls_nl_cosim_smoke"))
PY
)}"

REPORT="${OUT_DIR}/vitis_batch_report.json"
if [[ ! -f "${REPORT}" ]]; then
  echo "Expected report not found: ${REPORT}" >&2
  exit 1
fi

"${PYTHON_BIN}" - "${REPORT}" <<'PY'
import json
import sys
from pathlib import Path

report_path = Path(sys.argv[1])
report = json.loads(report_path.read_text(encoding="utf-8"))
summary = report["summary"]
print("\nCoSim smoke summary")
print("===================")
print(json.dumps(summary, indent=2))

for row in report["results"]:
    print(f"\nrecord={row['record_id']} top={row['top']} status={row['status']}")
    print(f"  log: {row.get('log')}")
    print(f"  verilog_files: {len(row.get('verilog_files', []))}")
    for item in row.get("verilog_files", [])[:8]:
        print(f"    - {item}")
    print(f"  cosim_artifacts: {len(row.get('cosim_artifacts', []))}")
    for item in row.get("cosim_artifacts", [])[:12]:
        print(f"    - {item}")
    tail = row.get("vitis_log_tail", "")
    if tail:
        print("\n  Vitis log tail")
        print("  --------------")
        for line in tail.splitlines():
            print(f"  {line}")
PY

echo
echo "Report: ${REPORT}"
echo "Per-row JSONL: ${OUT_DIR}/vitis_batch_results.jsonl"
