#!/usr/bin/env bash
# Is this checkout able to produce a trustworthy benchmark number?
#
# Run this in any existing clone BEFORE reusing it for a benchmark run. A checkout that
# predates the evidence fixes will happily print a pass@k built from runs that compared
# nothing -- 48% of the HLS-LeVeri suite did exactly that. This script does not fix
# anything; it tells you whether to pull.
#
#   bash scripts/check_checkout.sh
#
# Exit 0 = safe to run benchmarks. Exit 1 = pull before running anything.

set -uo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

red()  { printf '\033[31m%s\033[0m\n' "$*"; }
grn()  { printf '\033[32m%s\033[0m\n' "$*"; }
ylw()  { printf '\033[33m%s\033[0m\n' "$*"; }

fail=0

echo "== checkout =="
printf '  branch   : %s\n' "$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
printf '  commit   : %s\n' "$(git rev-parse --short HEAD 2>/dev/null || echo '?')"
dirty=$(git status --porcelain 2>/dev/null | wc -l | tr -d ' ')
printf '  uncommit : %s file(s)\n' "$dirty"

echo
echo "== the four fixes that make a number mean something =="
# Each marker is a string introduced by the commit that closed a vacuity route. Absent
# marker => that route is still open in this checkout.
check() {  # name, file, pattern
  if grep -q -- "$3" "$2" 2>/dev/null; then
    grn "  OK      $1"
  else
    red "  MISSING $1"
    fail=1
  fi
}
check "PhaseResult records what it compared   (589c8c8)" c2hlsc_agent/equivalence.py "comparisons: int | None"
check "multi-dimensional writes seen as output (06fa9d7)" c2hlsc_agent/analyze.py '\[\^\\\]\]+\\\])+'
check "oracle refuses a zero-comparison run   (589c8c8)" c2hlsc_agent/testgen.py "c2hlsc_comparisons == 0"
check "CoSim needs a positive verdict         (3bd0773)" c2hlsc_agent/cosim_verdict.py "COSIM_SUCCESS_MARKERS"

echo
echo "== benchmarks (third_party/ is gitignored; nothing ships with a clone) =="
for b in "HLS-LeVeri:third_party/HLS-LeVeri/HLS_LeVeri_benchmark.json:git clone --depth 1 https://github.com/cz-5f/HLS-LeVeri third_party/HLS-LeVeri" \
         "CHStone:third_party/CHStone:python3 scripts/fetch_chstone.py"; do
  name=${b%%:*}; rest=${b#*:}; path=${rest%%:*}; how=${rest#*:}
  if [ -e "$path" ]; then grn "  present $name"; else ylw "  absent  $name  ->  $how"; fi
done
[ -f data/hls_nl/hls_nl_repaired.accepted.jsonl ] \
  && grn "  present HLS_NL (tracked, ships with the clone)" \
  || ylw "  absent  HLS_NL  ->  unexpected; it is tracked in git"

echo
echo "== tools =="
for t in g++ python3 iverilog vvp klee vitis_hls; do
  p=$(command -v "$t" 2>/dev/null)
  if [ -n "$p" ]; then grn "  $t -> $p"; else ylw "  $t -> absent"; fi
done
echo "  (g++/python3 required; iverilog+vvp for the RTL tier; klee for refine;"
echo "   vitis_hls for csim/csynth/cosim -- without it those report blocked, not fail)"

echo
echo "== disk =="
df -h . | tail -1 | awk '{print "  avail: "$4"  used: "$5"   (~9 GB per 6-design run)"}'

echo
if [ "$fail" -ne 0 ]; then
  red "VERDICT: do NOT benchmark from this checkout."
  echo "  It is missing at least one fix that stops a run reporting pass having compared"
  echo "  nothing. Pull the branch first:"
  echo
  echo "    git fetch origin claude/agent-component-scaffold-5cr39w"
  echo "    git checkout claude/agent-component-scaffold-5cr39w"
  echo "    git pull --ff-only"
  echo "    pip install -e ."
  exit 1
fi
grn "VERDICT: this checkout carries all four evidence fixes."
echo "  Numbers from it can be trusted, provided you still check the per-phase"
echo "  'comparisons' count before quoting any pass@k -- see docs/RUNBOOK.md section 4."
