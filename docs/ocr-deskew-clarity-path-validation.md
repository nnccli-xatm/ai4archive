# OCR Deskew-Clarity Path Validation

Date: 2026-06-11

## Path

- Rule template: `ocr-preprocess-deskew-clarity-v1`
- Processing profile: `ocr_preprocess_deskew_clarity`
- Processing path: `ocr-preprocess-deskew-clarity-v1`
- Output profile: `ocr_preprocess_deskew_clarity`
- Main output suffix: `.ocr.png`
- Binary sidecar directory: `ocr_binary/`
- Color-to-grayscale opt-in: `ocr_force_grayscale=false` by default.

This path is an independent OCR route. It keeps the external API/CLI contract
unchanged and is selected through `--rule-template
ocr-preprocess-deskew-clarity-v1` or service job `rule_template`.

## Design

The path isolates the current suspected bottleneck: mandatory deskew can soften
small text and table lines. Instead of adding another stronger cleanup switch,
this route makes deskew itself measurable and selectable.

Processing behavior:

- Keep mandatory conservative deskew enabled by default.
- Preserve canvas size during deskew; do not crop or upscale the main output.
- Try multiple preserve-canvas rotation implementations for each corrected
  page:
  - Pillow bicubic
  - Pillow bilinear
  - OpenCV `INTER_LINEAR`
  - OpenCV `INTER_CUBIC`
  - OpenCV `INTER_LANCZOS4`
- Score candidates using text edge energy, soft-edge ratio, and detected
  horizontal/vertical table-line structure.
- Record the selected candidate and public-safe aggregate metrics in the
  processing manifest and quality summary.
- Reuse the structure-preserving OCR background normalization and binary
  sidecar logic after deskew, but keep independent output profile and reason
  codes.

## Regression Coverage

- Rule-template defaults and processing path mapping.
- Production CLI independent-path run on a rotated synthetic form page.
- Preserve-canvas deskew with no OCR supersampled size expansion.
- Manifest fields for selected candidate, candidate count, edge energy, soft
  edge ratio, and table-line score.
- OCR foreground retention and binary sidecar creation.
- Service job create/run/recover public-safe summary.
- Public capability and service capability allowlists.

## Real-Sample Validation

Command:

```powershell
$env:PYTHONPATH='src'; python -m archive_scan_qc.cli production-run --input "test image folder" --derivatives-out "generated\manual_test_test_images_20260611_ocr_deskew_clarity\derivatives" --metadata-out "generated\manual_test_test_images_20260611_ocr_deskew_clarity\metadata" --rule-template ocr-preprocess-deskew-clarity-v1 --ocr-force-grayscale --workers 1 --no-resume-processing
```

Actual local result directory:

- `generated/manual_test_测试图片_20260611_ocr_deskew_clarity`

Visual review sheet:

- `generated/manual_test_测试图片_20260611_ocr_deskew_clarity/ocr_deskew_clarity_contact_sheet.jpg`

Aggregate result:

- Status: `needs_review`
- Source images: 12
- Processed files: 12
- Failed files: 0
- Deskew operation files: 11
- OCR-preprocessed files: 10
- OCR binary sidecars: 12
- OCR review-required files: 3
- Source files modified: no
- Processing elapsed: 19.517402 seconds
- Throughput: 36.89 processed files/minute
- Selected deskew candidate: `opencv_lanczos4` for all deskewed pages

Quality metrics:

- `ocr_preprocess_changed_pixel_ratio`: min 0.0, avg 0.094245, max 0.262036
- `ocr_background_delta`: min 0.0, avg 1.666667, max 3.0
- `ocr_foreground_retention_ratio`: min 0.999998, avg 1.0, max 1.0
- `ocr_text_edge_energy_ratio`: min 0.887058, avg 0.952887, max 1.060272
- `ocr_text_soft_edge_ratio_delta`: min -0.080350, avg -0.040838, max 0.001807
- `ocr_binary_foreground_ratio`: min 0.015454, avg 0.098890, max 0.198590
- `ocr_binary_foreground_retention_ratio`: min 0.985453, avg 0.996653, max 1.0
- `ocr_deskew_clarity_candidate_count`: min 0.0, avg 4.583333, max 5.0
- `ocr_deskew_clarity_edge_energy`: min 0.0, avg 34.487707, max 48.212597
- `ocr_deskew_clarity_table_line_score`: min 0.0, avg 0.211518, max 0.901254

Comparison against previous real-sample runs:

- `ocr-preprocess-stroke-bg-v1` average `ocr_text_edge_energy_ratio`: 0.913637
- `ocr-preprocess-structure-v1` explicit grayscale average
  `ocr_text_edge_energy_ratio`: 0.942257
- `ocr-preprocess-deskew-clarity-v1` average
  `ocr_text_edge_energy_ratio`: 0.952887

## Visual Assessment

Manual review of the contact sheet found:

- No new visible water-ripple artifact on the sparse handwriting sample.
- Table and form lines remain visually straight and continuous.
- Main grayscale output remains conservative; it does not turn into a hard
  binary-looking derivative.
- The improvement over `ocr-preprocess-structure-v1` is measurable but modest.
- The path is slower because it evaluates multiple rotation candidates.

## Current Verdict

Keep `ocr-preprocess-deskew-clarity-v1` as the next experimental parallel OCR
path. It directly addresses deskew-induced softness and currently has the best
real-sample average text-edge energy among the recent conservative OCR paths,
but it still does not prove a decisive visual clarity improvement over the
original images. It should not become the default until OCR-engine metrics and
larger private sample validation confirm a meaningful recognition benefit.
