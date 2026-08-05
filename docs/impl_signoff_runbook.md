# Post-P&R sign-off runbook

How to get **measured** FPGA numbers (placed-and-routed resources + achieved clock)
instead of csynth estimates. Everything else in the pipeline stops at estimates.

Branch: `agent/fpga-post-impl-qor`. Needs **Vivado** on the Vitis host, not just Vitis HLS.

## Setup on a fresh machine

```bash
git clone https://github.com/huluk98/c2hlsc-agent.git
cd c2hlsc-agent
git checkout agent/fpga-post-impl-qor
pip install -e .
python3 -m unittest discover -s tests        # expect 437 tests OK
```

Use `python3` — there is no bare `python` on the Mac.

## Run it

Sign-off runs on a project that **already passed the ladder**. Two steps:

```bash
# 1. Produce a verified project (add --vitis-ssh here too if you want the full ladder)
python3 -m c2hlsc_agent.cli convert \
  --input examples/vector_add/input.c \
  --config examples/vector_add/config.yaml \
  --out /tmp/vadd --no-llm --run-vitis \
  --vitis-ssh USER@HOST

# 2. Vivado synthesis + place & route on that project
python3 -m c2hlsc_agent.cli impl --project /tmp/vadd --vitis-ssh USER@HOST
```

If `vitis_hls` is not on the remote PATH by default:

```bash
python3 -m c2hlsc_agent.cli impl --project /tmp/vadd \
  --vitis-ssh USER@HOST \
  --vitis-setup 'source /tools/Xilinx/Vitis/2024.2/settings64.sh'
```

Local Vitis instead of SSH: drop `--vitis-ssh`, optionally pass `--vitis-bin`.

**Expect it to take minutes to tens of minutes.** That is P&R, not a hang.

## Output

Writes `impl_report.json` in the project dir:

```json
{
  "status": "pass",
  "top": "vector_add",
  "part": "xczu7ev-ffvc1156-2-e",
  "target_clock_ns": 10.0,
  "report_path": "c2hlsc_project/solution1/impl/report/verilog/export_impl.rpt",
  "impl": { "lut": 138, "ff": 156, "dsp": 0, "bram": 0, "srl": 2,
            "uram": 0, "slice": 41,
            "cp_achieved_ns": 3.276, "cp_required_ns": 10.0 }
}
```

`cp_achieved_ns` is the real post-implementation critical path. Compare it against
`target_clock_ns`, not against csynth's `estimated_clock_ns`.

## Guards you will hit (all intentional)

| Message | Meaning |
|---|---|
| `refusing post-implementation sign-off: ... status is 'fail'` | The design did not pass the ladder. Fix it, or pass `--allow-unverified` if you knowingly want numbers for an unverified design. |
| `run_impl.tcl is missing` | Project predates this feature. Re-run `convert`. |
| `no post-implementation results found in ...` | The report exists but has no parseable result block — see below. |
| `export_design reported success but no report was found` | Report landed somewhere the glob missed — see below. |

## If parsing fails on the first real run

**This is the one part never tested against real Vitis output.** The runner, CLI,
guards, and remote push/pull are all verified; the report *format* is not.

Fix is small and local to `c2hlsc_agent/qor.py`:

1. Find the real report:
   `find <project>/c2hlsc_project -path '*impl/report*' -name '*.rpt'`
2. If the path shape differs → widen the glob in `find_impl_report()`.
3. If the field names differ → adjust `_IMPL_RESOURCE_KEYS` and the two
   `CP achieved post-implementation` / `CP required` regexes in `parse_impl_report()`.
4. Paste the real report body into `EXPORT_IMPL_RPT` in
   `tests/test_impl_signoff.py` so it is pinned from then on.

Nothing else needs to change.

## Design constraint — do not "fix" this

`impl` is deliberately **not** in `PHASE_ORDER`, `run_vitis`, or `verify_project`, and
its numbers stay out of `area_proxy`. P&R takes minutes to tens of minutes; admitting it
to the acceptance ladder puts it inside the QoR optimizer's per-candidate loop, and
putting it in `area_proxy` makes the candidate search score against a metric that costs
a Vivado run to evaluate. Both are pinned by tests in `tests/test_impl_signoff.py`.
