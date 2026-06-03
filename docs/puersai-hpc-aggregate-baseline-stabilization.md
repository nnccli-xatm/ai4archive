# Puersai HPC Aggregate Baseline Stabilization

This note records the regression coverage added for the aggregate-only baseline
runner. The coverage is intentionally synthetic and does not depend on private
sample files, private paths, OCR text, thumbnails, hashes, row-level findings, or
image content.

## Scope

- Verify the aggregate baseline schema remains stable for the target environment
  label used by the puersai-hpc validation flow.
- Verify scan and processing phase timings stay present in the public summary.
- Verify the privacy leak detector catches path-like values, sensitive file
  extensions, and hash-like strings.
- Verify cleanup removes generated private artifacts while preserving the input
  sample directory and the public aggregate summary.
- Verify cleanup does not expose preserved private sample paths in the public
  cleanup result.

## Validation

Run the focused regression test with:

```powershell
python tests\test_aggregate_baseline_regression.py
```

The test file uses temporary directories and synthetic placeholder names only.
The validation target for a real private run remains the aggregate baseline
command configured by the operator environment.
