#!/usr/bin/env bash
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"

usage() {
  cat >&2 <<'USAGE'
Usage:
  VITIS_HLS_BIN=/opt/Xilinx/Vitis_HLS/2023.2/bin/vitis_hls \
    bash scripts/run_hls_nl_vitis_triage.sh

Optional environment overrides:
  HLS_NL_JSONL            Input JSONL, default data/hls_nl/hls_nl_repaired.accepted.jsonl
  HLS_NL_RUN_ROOT         Output root, default runs/hls_nl_vitis_triage_<timestamp>
  HLS_NL_OFFSET           Starting record offset for the first pass, default 0
  HLS_NL_LIMIT            Maximum records for the first pass, default all
  HLS_NL_CSYNTH_RESULTS   Existing CSim+CSynth vitis_batch_results.jsonl to reuse
  HLS_NL_COSIM_RESULTS    Existing CoSim vitis_batch_results.jsonl to reuse
  HLS_NL_CSYNTH_TIMEOUT   CSim+CSynth timeout seconds per row, default 300
  HLS_NL_COSIM_TIMEOUT    CSim/CSynth/CoSim timeout seconds per phase, default 300
  HLS_NL_LOG_TAIL_LINES   Log tail lines stored in JSON reports, default 160
  VITIS_PART              Vitis part, default xczu7ev-ffvc1156-2-e
  VITIS_CLOCK             Clock period ns, default 10
  VITIS_HLS_BIN           Full path to vitis_hls; if unset, PATH is used
USAGE
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 2
fi

cd "${REPO_ROOT}"

INPUT="${HLS_NL_JSONL:-data/hls_nl/hls_nl_repaired.accepted.jsonl}"
RUN_ROOT="${HLS_NL_RUN_ROOT:-runs/hls_nl_vitis_triage_$(date +%Y%m%d_%H%M%S)}"
CSYNTH_DIR="${RUN_ROOT}/csynth"
COSIM_DIR="${RUN_ROOT}/cosim"
CSYNTH_RUN_DIR="${RUN_ROOT}/csynth_new"
COSIM_RUN_DIR="${RUN_ROOT}/cosim_new"
PASS_JSONL="${RUN_ROOT}/hls_nl_csynth_pass.jsonl"
CSYNTH_INPUT="${RUN_ROOT}/hls_nl_csynth_remaining.jsonl"
COSIM_INPUT="${RUN_ROOT}/hls_nl_cosim_remaining.jsonl"
SUMMARY_JSON="${RUN_ROOT}/triage_summary.json"
ATTENTION_JSONL="${RUN_ROOT}/cosim_attention.jsonl"
CSYNTH_RESULTS="${CSYNTH_DIR}/vitis_batch_results.jsonl"
COSIM_RESULTS="${COSIM_DIR}/vitis_batch_results.jsonl"
OFFSET="${HLS_NL_OFFSET:-0}"
CSYNTH_TIMEOUT="${HLS_NL_CSYNTH_TIMEOUT:-300}"
COSIM_TIMEOUT="${HLS_NL_COSIM_TIMEOUT:-300}"
LOG_TAIL_LINES="${HLS_NL_LOG_TAIL_LINES:-160}"
PART="${VITIS_PART:-xczu7ev-ffvc1156-2-e}"
CLOCK="${VITIS_CLOCK:-10}"

if [[ ! -f "${INPUT}" ]]; then
  echo "Input JSONL not found: ${INPUT}" >&2
  exit 2
fi

VITIS_ARGS=()
if [[ -n "${VITIS_HLS_BIN:-}" ]]; then
  if [[ ! -x "${VITIS_HLS_BIN}" ]]; then
    echo "VITIS_HLS_BIN is not executable: ${VITIS_HLS_BIN}" >&2
    exit 2
  fi
  VITIS_ARGS+=(--vitis-hls-bin "${VITIS_HLS_BIN}")
elif ! command -v vitis_hls >/dev/null 2>&1; then
  echo "vitis_hls not found. Set VITIS_HLS_BIN=/path/to/vitis_hls." >&2
  exit 2
fi

mkdir -p "${RUN_ROOT}"

CSYNTH_SEED_RESULTS="${HLS_NL_CSYNTH_RESULTS:-}"
if [[ -z "${CSYNTH_SEED_RESULTS}" && -f "${CSYNTH_RESULTS}" ]]; then
  CSYNTH_SEED_RESULTS="${CSYNTH_RESULTS}"
fi
COSIM_SEED_RESULTS="${HLS_NL_COSIM_RESULTS:-}"
if [[ -z "${COSIM_SEED_RESULTS}" && -f "${COSIM_RESULTS}" ]]; then
  COSIM_SEED_RESULTS="${COSIM_RESULTS}"
fi
CSYNTH_SKIP_RESULTS="${CSYNTH_SEED_RESULTS:-${COSIM_SEED_RESULTS:-}}"

echo "AUTO RTL HLS_NL Vitis triage"
echo "============================"
echo "input:       ${INPUT}"
echo "run root:    ${RUN_ROOT}"
echo "part:        ${PART}"
echo "clock:       ${CLOCK}"
echo "csynth t/o:  ${CSYNTH_TIMEOUT}s per row"
echo "cosim t/o:   ${COSIM_TIMEOUT}s per phase"
if [[ -n "${HLS_NL_LIMIT:-}" ]]; then
  echo "limit:       ${HLS_NL_LIMIT}"
else
  echo "limit:       all"
fi
if [[ -n "${CSYNTH_SEED_RESULTS}" ]]; then
  echo "reuse csyn:  ${CSYNTH_SEED_RESULTS}"
fi
if [[ -n "${COSIM_SEED_RESULTS}" ]]; then
  echo "reuse cosim: ${COSIM_SEED_RESULTS}"
fi
echo

"${PYTHON_BIN}" - "${INPUT}" "${CSYNTH_SKIP_RESULTS}" "${CSYNTH_INPUT}" "${OFFSET}" "${HLS_NL_LIMIT:-}" <<'PY'
import json
import sys
from pathlib import Path

DONE_STATUSES = {"pass", "fail", "timeout", "fail_no_verilog"}

source = Path(sys.argv[1])
seed_arg = sys.argv[2]
out = Path(sys.argv[3])
offset = int(sys.argv[4] or 0)
limit = int(sys.argv[5]) if sys.argv[5] else None

done_ids = set()
if seed_arg:
    seed = Path(seed_arg)
    if not seed.exists():
        raise SystemExit(f"Existing CSim+CSynth results not found: {seed}")
    for line in seed.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("status") in DONE_STATUSES:
            done_ids.add(int(row["record_id"]))

selected = []
with source.open(encoding="utf-8") as f:
    for fallback, line in enumerate(f):
        if fallback < offset:
            continue
        if limit is not None and len(selected) >= limit:
            break
        record = json.loads(line)
        try:
            record_id = int(record.get("record_id", fallback))
        except (TypeError, ValueError):
            record_id = fallback
        if "record_id" not in record:
            record["record_id"] = record_id
        selected.append((record_id, record))

out.parent.mkdir(parents=True, exist_ok=True)
remaining = 0
with out.open("w", encoding="utf-8") as f:
    for record_id, record in selected:
        if record_id in done_ids:
            continue
        f.write(json.dumps(record, sort_keys=True) + "\n")
        remaining += 1

print(
    f"CSim+CSynth resume: selected={len(selected)} "
    f"already_done={len([1 for record_id, _ in selected if record_id in done_ids])} "
    f"remaining={remaining}"
)
PY

CSYNTH_ARGS=(
  --input "${CSYNTH_INPUT}"
  --out-dir "${CSYNTH_RUN_DIR}"
  --part "${PART}"
  --clock "${CLOCK}"
  --timeout-seconds "${CSYNTH_TIMEOUT}"
  --log-tail-lines "${LOG_TAIL_LINES}"
  "${VITIS_ARGS[@]}"
)

if [[ -s "${CSYNTH_INPUT}" ]]; then
  echo "Phase 1: fast CSim+CSynth triage"
  printf '  %q' "${PYTHON_BIN}" scripts/run_hls_nl_vitis_batch.py "${CSYNTH_ARGS[@]}"
  printf '\n'
  if ! "${PYTHON_BIN}" scripts/run_hls_nl_vitis_batch.py "${CSYNTH_ARGS[@]}"; then
    echo "CSim+CSynth triage completed with failing/timeout rows; continuing with passing rows."
  fi
else
  echo "Phase 1: no new CSim+CSynth rows to run; reusing existing results."
fi

"${PYTHON_BIN}" - "${CSYNTH_SEED_RESULTS}" "${CSYNTH_RUN_DIR}/vitis_batch_results.jsonl" "${CSYNTH_RESULTS}" <<'PY'
import json
import sys
from pathlib import Path

seed_arg, new_arg, out_arg = sys.argv[1:4]
out = Path(out_arg)
rows_by_id = {}

for item in (seed_arg, new_arg):
    if not item:
        continue
    path = Path(item)
    if not path.exists():
        continue
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        rows_by_id[int(row["record_id"])] = row

out.parent.mkdir(parents=True, exist_ok=True)
with out.open("w", encoding="utf-8") as f:
    for record_id in sorted(rows_by_id):
        f.write(json.dumps(rows_by_id[record_id], sort_keys=True) + "\n")

print(f"Merged CSim+CSynth results: {len(rows_by_id)} -> {out}")
PY

"${PYTHON_BIN}" - "${INPUT}" "${CSYNTH_RESULTS}" "${PASS_JSONL}" "${OFFSET}" "${HLS_NL_LIMIT:-}" <<'PY'
import json
import sys
from pathlib import Path

source = Path(sys.argv[1])
results = Path(sys.argv[2])
out = Path(sys.argv[3])
offset = int(sys.argv[4] or 0)
limit = int(sys.argv[5]) if sys.argv[5] else None

passed_ids = set()
for line in results.read_text(encoding="utf-8").splitlines():
    row = json.loads(line)
    if row.get("status") == "pass":
        passed_ids.add(int(row["record_id"]))

out.parent.mkdir(parents=True, exist_ok=True)
written = 0
with source.open(encoding="utf-8") as src, out.open("w", encoding="utf-8") as dst:
    for fallback, line in enumerate(src):
        if fallback < offset:
            continue
        if limit is not None and fallback >= offset + limit:
            break
        record = json.loads(line)
        try:
            record_id = int(record.get("record_id", fallback))
        except (TypeError, ValueError):
            record_id = fallback
        if record_id in passed_ids:
            dst.write(json.dumps(record, sort_keys=True) + "\n")
            written += 1

print(f"Filtered CSim+CSynth passing rows: {written}")
PY

"${PYTHON_BIN}" - "${PASS_JSONL}" "${COSIM_SEED_RESULTS}" "${COSIM_INPUT}" <<'PY'
import json
import sys
from pathlib import Path

DONE_STATUSES = {"pass", "fail", "timeout", "fail_no_verilog"}

source = Path(sys.argv[1])
seed_arg = sys.argv[2]
out = Path(sys.argv[3])

done_ids = set()
if seed_arg:
    seed = Path(seed_arg)
    if not seed.exists():
        raise SystemExit(f"Existing CoSim results not found: {seed}")
    for line in seed.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("status") in DONE_STATUSES:
            done_ids.add(int(row["record_id"]))

selected = []
if source.exists():
    for line in source.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        selected.append((int(record["record_id"]), record))

out.parent.mkdir(parents=True, exist_ok=True)
remaining = 0
with out.open("w", encoding="utf-8") as f:
    for record_id, record in selected:
        if record_id in done_ids:
            continue
        f.write(json.dumps(record, sort_keys=True) + "\n")
        remaining += 1

print(
    f"CoSim resume: selected={len(selected)} "
    f"already_done={len([1 for record_id, _ in selected if record_id in done_ids])} "
    f"remaining={remaining}"
)
PY

if [[ ! -s "${PASS_JSONL}" ]]; then
  echo "No CSim+CSynth passing rows; skipping CoSim." >&2
else
  COSIM_ARGS=(
    --input "${COSIM_INPUT}"
    --out-dir "${COSIM_RUN_DIR}"
    --run-full-cosim
    --part "${PART}"
    --clock "${CLOCK}"
    --timeout-seconds "${COSIM_TIMEOUT}"
    --log-tail-lines "${LOG_TAIL_LINES}"
    "${VITIS_ARGS[@]}"
  )

  if [[ -s "${COSIM_INPUT}" ]]; then
    echo
    echo "Phase 2: split CSim+CSynth+CoSim on passing rows"
    printf '  %q' "${PYTHON_BIN}" scripts/run_hls_nl_vitis_batch.py "${COSIM_ARGS[@]}"
    printf '\n'
    if ! "${PYTHON_BIN}" scripts/run_hls_nl_vitis_batch.py "${COSIM_ARGS[@]}"; then
      echo "CoSim triage completed with failing/timeout rows; see attention list below."
    fi
  else
    echo "Phase 2: no new CoSim rows to run; reusing existing results."
  fi
fi

"${PYTHON_BIN}" - "${COSIM_SEED_RESULTS}" "${COSIM_RUN_DIR}/vitis_batch_results.jsonl" "${COSIM_RESULTS}" <<'PY'
import json
import sys
from pathlib import Path

seed_arg, new_arg, out_arg = sys.argv[1:4]
out = Path(out_arg)
rows_by_id = {}

for item in (seed_arg, new_arg):
    if not item:
        continue
    path = Path(item)
    if not path.exists():
        continue
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        rows_by_id[int(row["record_id"])] = row

out.parent.mkdir(parents=True, exist_ok=True)
with out.open("w", encoding="utf-8") as f:
    for record_id in sorted(rows_by_id):
        f.write(json.dumps(rows_by_id[record_id], sort_keys=True) + "\n")

print(f"Merged CoSim results: {len(rows_by_id)} -> {out}")
PY

"${PYTHON_BIN}" - "${CSYNTH_RESULTS}" "${COSIM_RESULTS}" "${SUMMARY_JSON}" "${ATTENTION_JSONL}" <<'PY'
import collections
import json
import sys
from pathlib import Path

csynth_path = Path(sys.argv[1])
cosim_path = Path(sys.argv[2])
summary_path = Path(sys.argv[3])
attention_path = Path(sys.argv[4])

def summarize(path: Path):
    status = collections.Counter()
    failed_phase = collections.Counter()
    rows = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            rows.append(row)
            status[row.get("status", "unknown")] += 1
            failed_phase[row.get("failed_phase", "pass_or_unset")] += 1
    return rows, dict(status), dict(failed_phase)

csynth_rows, csynth_status, csynth_failed = summarize(csynth_path)
cosim_rows, cosim_status, cosim_failed = summarize(cosim_path)

attention = []
for row in cosim_rows:
    if row.get("status") != "pass":
        attention.append(
            {
                "record_id": row.get("record_id"),
                "top": row.get("top"),
                "status": row.get("status"),
                "failed_phase": row.get("failed_phase"),
                "log": row.get("log"),
                "phase_logs": {
                    name: phase.get("log")
                    for name, phase in row.get("phases", {}).items()
                    if isinstance(phase, dict)
                },
            }
        )

summary = {
    "csynth": {
        "results": len(csynth_rows),
        "status_counts": csynth_status,
        "failed_phase_counts": csynth_failed,
    },
    "cosim": {
        "results": len(cosim_rows),
        "status_counts": cosim_status,
        "failed_phase_counts": cosim_failed,
        "attention_count": len(attention),
    },
    "outputs": {
        "csynth_results": str(csynth_path),
        "cosim_results": str(cosim_path),
        "attention_jsonl": str(attention_path),
    },
}

summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
with attention_path.open("w", encoding="utf-8") as f:
    for item in attention:
        f.write(json.dumps(item, sort_keys=True) + "\n")

print()
print("Triage summary")
print("==============")
print(json.dumps(summary, indent=2))
if attention:
    print()
    print(f"Inspect slow/failing CoSim cases in: {attention_path}")
    for item in attention[:20]:
        print(
            f"  record={item['record_id']} top={item['top']} "
            f"status={item['status']} failed_phase={item['failed_phase']} log={item['log']}"
        )
    if len(attention) > 20:
        print(f"  ... {len(attention) - 20} more")
PY

echo
echo "Done."
echo "Run root: ${RUN_ROOT}"
echo "Summary:  ${SUMMARY_JSON}"
echo "Attention list: ${ATTENTION_JSONL}"
