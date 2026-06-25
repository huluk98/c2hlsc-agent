# HLS_NL Input Data

This directory contains the accepted repaired HLS_NL JSONL input used by the
Vitis batch and full-CoSim export flow.

- `hls_nl_repaired.accepted.jsonl`: accepted repaired HLS_NL records.

Run the full dataset through Vitis HLS C/RTL CoSim with:

```bash
VITIS_HLS_BIN="/path/to/Vitis_HLS/2024.2/bin/vitis_hls" \
python scripts/run_hls_nl_vitis_batch.py \
  --config configs/hls_nl_full_cosim.json
```

Then export only full-CoSim passing cases to the top-level corpus:

```bash
python scripts/export_cosim_successes.py \
  --report build/hls_nl_accepted_full_cosim/vitis_batch_report.json \
  --out-dir hls_nl_full_cosim_passes
```
