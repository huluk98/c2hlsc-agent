# HLS_NL Full-CoSim Passes

This top-level directory is reserved for the uploadable corpus of HLS_NL cases
that pass full Vitis HLS C/RTL CoSim.

Generate it with:

```bash
python scripts/export_cosim_successes.py \
  --report build/hls_nl_accepted_full_cosim/vitis_batch_report.json \
  --out-dir hls_nl_full_cosim_passes
```

The exporter replaces this placeholder with only passing cases plus compact
evidence files. Failed and skipped rows are retained as JSONL manifests for
inspection.
