# HLS_NL Full-CoSim Passes

This top-level directory is reserved for the uploadable corpus of HLS_NL cases
that pass full Vitis HLS C/RTL CoSim.

Generate it with:

```bash
VITIS_HLS_BIN="/path/to/Vitis_HLS/2024.2/bin/vitis_hls" \
python scripts/run_hls_nl_vitis_batch.py \
  --config configs/hls_nl_full_cosim.json
```

Then export only the passing cases into this top-level directory:

```bash
python scripts/export_cosim_successes.py \
  --report build/hls_nl_accepted_full_cosim/vitis_batch_report.json \
  --out-dir hls_nl_full_cosim_passes
```

The exporter replaces this placeholder with only passing cases plus compact
evidence files. Failed and skipped rows are retained as JSONL manifests for
inspection.
