# OCR Stroke-Protected Background Path Validation

Date: 2026-06-11

## Path

- Rule template: `ocr-preprocess-stroke-bg-v1`
- Processing profile: `ocr_preprocess_stroke_bg`
- Processing path: `ocr-preprocess-stroke-bg-v1`
- Output profile: `ocr_preprocess_stroke_bg`
- Main output suffix: `.ocr.png`
- Binary sidecar directory: `ocr_binary/`
- Color-to-grayscale opt-in: `ocr_force_grayscale=false` by default;
  CLI callers must pass `--ocr-force-grayscale` to allow true color input pages
  to be converted to grayscale in the main derivative.

This path is an independent OCR preprocessing route. It does not replace
`ocr-preprocess-leptonica-v1`, `ocr-preprocess-opencv-local-v1`, or
`ocr-preprocess-sauvola-wolf-v1`. External callers keep the same interface and
select the path through `--rule-template` or service job `rule_template`.

## Design

The path is built for real scanned document images where OCR preprocessing must
not destroy text or table strokes.

Processing behavior:

- Preserve source canvas size during deskew.
- Preserve true color main derivatives by default. When a page contains actual
  color content, OCR grayscale preprocessing records
  `color_input_requires_explicit_grayscale` and leaves the main `.ocr.png` in
  color unless `ocr_force_grayscale` is explicitly enabled.
- Do not crop, upscale, or hard-threshold the grayscale main image.
- Detect protected strokes from local contrast, Sobel gradients, connected
  components, and horizontal/vertical line morphology.
- Normalize only background pixels outside the protected stroke/table mask.
- Write a conservative Otsu/adaptive binary sidecar for OCR and review.
- Record `processing_path`, operation names, OCR metrics, timings, and review
  reason codes in the existing manifest and public-safe summaries.

## Implementation

Touched public and service boundaries:

- `src/archive_scan_qc/processing_paths.py`
- `src/archive_scan_qc/rules.py`
- `src/archive_scan_qc/rule_templates.py`
- `src/archive_scan_qc/processing.py`
- `src/archive_scan_qc/cli.py`
- `src/archive_scan_qc/public_capability_contract.py`
- `src/archive_scan_qc/service_api.py`
- `src/archive_scan_qc/service_jobs.py`

Regression coverage:

- Rule-template defaults and processing path mapping.
- Production CLI independent-path run.
- Service job create/run/recover public-safe summary.
- Public capability and service capability allowlists.

## Real-Sample Validation

Command:

```powershell
$env:PYTHONPATH='src'; python -m archive_scan_qc.cli production-run --input "测试图片" --derivatives-out "generated\manual_test_测试图片_20260611_ocr_stroke_bg_final\derivatives" --metadata-out "generated\manual_test_测试图片_20260611_ocr_stroke_bg_final\metadata" --rule-template ocr-preprocess-stroke-bg-v1 --workers 1 --no-resume-processing
```

Result directory:

`generated/manual_test_测试图片_20260611_ocr_stroke_bg_final`

Aggregate result:

- Status: `needs_review`
- Source images: 12
- Processed files: 12
- Failed files: 0
- Deskewed files: 11
- OCR-preprocessed files: 10
- OCR binary sidecars: 12
- OCR review-required files: 3
- Guardrail failed files: 0
- Output size mismatches: 0
- Supersampled deskew files: 0
- Source files modified: no

Quality metrics:

- `ocr_preprocess_changed_pixel_ratio`: min 0.0, avg 0.058752, max 0.230436
- `ocr_foreground_retention_ratio`: min 0.999998, avg 1.0, max 1.0
- `ocr_text_edge_energy_ratio`: min 0.852940, avg 0.913637, max 1.0
- `ocr_text_soft_edge_ratio_delta`: min -0.105934, avg -0.040695, max 0.0
- `ocr_binary_foreground_ratio`: min 0.013035, avg 0.084334, max 0.163582
- `ocr_binary_foreground_retention_ratio`: min 0.988731, avg 0.997369, max 1.0

Reason-code distribution:

- OCR preprocess: `applied_stroke_bg_background_normalization` 10,
  `low_confidence_background` 2
- Binary: `applied_stroke_bg_otsu_threshold` 12
- OCR review codes: `low_confidence_background` 2,
  `color_rich_document_review` 1, `red_mark_review` 1

Performance:

- Scan elapsed: 3.994006 seconds
- Processing elapsed: 8.909223 seconds
- Throughput: 80.82 processed files/minute

## Assessment

This path is safer than the rejected aggressive local-threshold experiments:

- It does not create the visible Sauvola/Wolf water-ripple artifact on the sparse
  handwriting sample.
- It keeps output dimensions equal to source dimensions.
- It keeps foreground retention effectively at 1.0.
- It does not trigger processing guardrail failures.

It does not solve the core clarity problem:

- Average `ocr_text_edge_energy_ratio` remains below 1.0.
- The result is visually conservative; it cleans and whitens background but does
  not materially improve text edge clarity.
- The metric pattern supports the earlier finding that mandatory deskew can
  soften text edges even when the subsequent background normalization preserves
  strokes.

Therefore `ocr-preprocess-stroke-bg-v1` should remain an experimental parallel
path for comparison, not a default or promoted OCR route.

## Rejected Experiments

Two attempts from this implementation cycle should not be promoted:

- Mask-limited dark stroke restoration: increased pixel-change ratio and binary
  foreground density, but did not improve average edge-energy and worsened
  soft-edge metrics.
- Preserve-canvas internal supersampled deskew: kept output size stable but
  lowered average edge-energy on the real sample set and reduced throughput.

## Next Focus

The next image-quality effort should target deskew clarity directly, not another
background-cleaning switch. Candidate work:

- Deskew interpolation comparison on the same real sample set, including
  nearest, bilinear, bicubic, Lanczos, OpenCV warpAffine variants, and
  text-mask-aware restoration.
- Separate deskew quality metrics from background-normalization metrics so the
  pipeline can prove whether clarity loss happens before OCR preprocessing.
- Add table-line continuity and stroke-width stability metrics to real-sample
  private validation.
- Keep all candidates behind independent `processing_path` IDs and require
  same-size output, source immutability, public-safe summaries, and real-sample
  before/after evidence.
