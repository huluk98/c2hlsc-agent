# HLS_NL Input Data

This directory contains the accepted repaired HLS_NL JSONL input used by the
Vitis batch and full-CoSim export flow.

- `hls_nl_repaired.accepted.jsonl`: accepted repaired HLS_NL records. Records
  with syntactic unconditional forever loops are kept out of this accepted set
  and listed in the repair deletion manifest with code-free metadata.

Run the full dataset through Vitis HLS C/RTL CoSim with:

```bash
VITIS_HLS_BIN="/path/to/Vitis_HLS/2024.2/bin/vitis_hls" \
python scripts/run_hls_nl_vitis_batch.py \
  --config configs/hls_nl_full_cosim.json
```

Full-CoSim batch runs execute CSim, CSynth, and CoSim as separate Vitis
invocations. Timeout and failure rows include `failed_phase`, per-phase status,
and per-phase logs, which makes CoSim hangs easier to distinguish from CSim or
synthesis failures. The full-corpus timeout is 900 seconds per phase; the smoke
config is 300 seconds per phase.

Then export only full-CoSim passing cases to the top-level corpus:

```bash
python scripts/export_cosim_successes.py \
  --report build/hls_nl_accepted_full_cosim/vitis_batch_report.json \
  --out-dir hls_nl_full_cosim_passes
```
