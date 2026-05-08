#!/usr/bin/env sh
set -eu

# Privacy-safe command template. Replace the three directory variables with
# approved local paths before use; do not commit real collection paths.
INPUT_DIR="${INPUT_DIR:-/path/to/synthetic-or-approved-input}"
REPORT_DIR="${REPORT_DIR:-/path/to/qc-report-output}"
PROCESS_DIR="${PROCESS_DIR:-/path/to/processed-derivatives}"

archive-scan-qc preflight \
  --input "$INPUT_DIR" \
  --out "$REPORT_DIR" \
  --process-out "$PROCESS_DIR" \
  --auto-crop \
  --deskew \
  --trim-dark-border \
  --despeckle \
  --workers "${WORKERS:-2}" \
  --project "example-project" \
  --batch "example-batch-001" \
  --manifest-csv "examples/manifest.sample.csv" \
  --rules-profile "examples/rules-profile.production-sample.json"

archive-scan-qc \
  --input "$INPUT_DIR" \
  --out "$REPORT_DIR" \
  --process-out "$PROCESS_DIR" \
  --auto-crop \
  --deskew \
  --trim-dark-border \
  --despeckle \
  --workers "${WORKERS:-2}" \
  --project "example-project" \
  --batch "example-batch-001" \
  --manifest-csv "examples/manifest.sample.csv" \
  --rules-profile "examples/rules-profile.production-sample.json"
