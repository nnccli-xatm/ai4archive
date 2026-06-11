# OCR Structure-Preserving Path Validation

Date: 2026-06-11

## Path

- Rule template: `ocr-preprocess-structure-v1`
- Processing profile: `ocr_preprocess_structure`
- Processing path: `ocr-preprocess-structure-v1`
- Output profile: `ocr_preprocess_structure`
- Main output suffix: `.ocr.png`
- Binary sidecar directory: `ocr_binary/`
- Color-to-grayscale opt-in: `ocr_force_grayscale=false` by default. CLI callers
  must pass `--ocr-force-grayscale` when they intentionally want true color
  pages converted to grayscale for OCR preprocessing.

This is an independent OCR route. It does not replace
`ocr-preprocess-leptonica-v1`, `ocr-preprocess-opencv-local-v1`,
`ocr-preprocess-sauvola-wolf-v1`, or `ocr-preprocess-stroke-bg-v1`.
External callers keep the same API/CLI shape and select this route through
`--rule-template ocr-preprocess-structure-v1` or service job `rule_template`.

## Design

The route targets document pages where OCR preprocessing must preserve text
strokes and table/grid lines before any attempt to whiten or normalize the
background.

Processing behavior:

- Use the existing conservative deskew flow, with preserve-canvas OCR deskew so
  output pixel dimensions match the input dimensions.
- Preserve true color main derivatives by default. When grayscale conversion is
  not explicitly allowed, color pages can still produce binary sidecars, but
  grayscale enhancement records review reason codes instead of silently
  discarding color.
- Do not crop, upscale, or hard-threshold the grayscale main output.
- Estimate background with large-kernel morphology and blur.
- Build a protected structure mask from local contrast, Sobel gradients,
  connected components, and multi-scale horizontal/vertical morphology.
- Apply background whitening/lift only outside the protected mask.
- Keep protected text/table pixels close to their original values.
- Generate an OCR binary sidecar with Otsu/adaptive thresholding and explicit
  detected-line foreground preservation.

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
- Same-size output and source immutability.
- Foreground retention and text-edge energy thresholds.
- Table-line dark-pixel retention on synthetic ruled-form input.
- Binary sidecar creation and reason-code allowlist.
- Service job create/run/recover public-safe summary.
- Public capability and service capability allowlists.

## Real-Sample Validation

Default color-safe command:

```powershell
$env:PYTHONPATH='src'; python -m archive_scan_qc.cli production-run --input "test image folder" --derivatives-out "generated\manual_test_test_images_20260611_ocr_structure\derivatives" --metadata-out "generated\manual_test_test_images_20260611_ocr_structure\metadata" --rule-template ocr-preprocess-structure-v1 --workers 1 --no-resume-processing
```

Explicit grayscale OCR preprocessing command:

```powershell
$env:PYTHONPATH='src'; python -m archive_scan_qc.cli production-run --input "test image folder" --derivatives-out "generated\manual_test_test_images_20260611_ocr_structure_gray\derivatives" --metadata-out "generated\manual_test_test_images_20260611_ocr_structure_gray\metadata" --rule-template ocr-preprocess-structure-v1 --ocr-force-grayscale --workers 1 --no-resume-processing
```

Actual local result directories:

- `generated/manual_test_测试图片_20260611_ocr_structure`
- `generated/manual_test_测试图片_20260611_ocr_structure_gray`

Visual review sheet:

- `generated/manual_test_测试图片_20260611_ocr_structure_gray/ocr_structure_contact_sheet.jpg`

### Default Color-Safe Run

- Status: `needs_review`
- Source images: 12
- Processed files: 12
- Failed files: 0
- Deskew operation files: 11
- OCR-preprocessed files: 7
- OCR binary sidecars: 12
- OCR review-required files: 5
- Guardrail failed files: 0
- Output size mismatches: 0
- Source files modified: no

Quality metrics:

- `ocr_preprocess_changed_pixel_ratio`: min 0.0, avg 0.085245, max 0.276491
- `ocr_foreground_retention_ratio`: min 1.0, avg 1.0, max 1.0
- `ocr_text_edge_energy_ratio`: min 0.871297, avg 0.955258, max 1.021090
- `ocr_text_soft_edge_ratio_delta`: min -0.105675, avg -0.029655, max 0.0
- `ocr_binary_foreground_ratio`: min 0.015433, avg 0.097560, max 0.195503
- `ocr_binary_foreground_retention_ratio`: min 0.986406, avg 0.996915, max 1.0

Reason-code distribution:

- OCR preprocess: `applied_structure_preserving_background_normalization`,
  `color_input_requires_explicit_grayscale`, `low_confidence_background`
- Review codes: `color_input_requires_explicit_grayscale`,
  `color_rich_document_review`, `low_confidence_background`, `red_mark_review`

### Explicit Grayscale Run

- Status: `needs_review`
- Source images: 12
- Processed files: 12
- Failed files: 0
- Deskew operation files: 11
- OCR-preprocessed files: 10
- OCR binary sidecars: 12
- OCR review-required files: 3
- Guardrail failed files: 0
- Output size mismatches: 0
- Source files modified: no
- Processing elapsed: 16.950001 seconds
- Throughput: 42.48 processed files/minute
- Deskew elapsed: 8.365086 seconds

Quality metrics:

- `ocr_preprocess_changed_pixel_ratio`: min 0.0, avg 0.102231, max 0.276491
- `ocr_foreground_retention_ratio`: min 0.999998, avg 1.0, max 1.0
- `ocr_text_edge_energy_ratio`: min 0.871297, avg 0.942257, max 1.060272
- `ocr_text_soft_edge_ratio_delta`: min -0.105675, avg -0.042537, max 0.001807
- `ocr_binary_foreground_ratio`: min 0.015433, avg 0.098533, max 0.195503
- `ocr_binary_foreground_retention_ratio`: min 0.986406, avg 0.996808, max 1.0

Reason-code distribution:

- OCR preprocess: `applied_structure_preserving_background_normalization`,
  `low_confidence_background`
- Review codes: `color_rich_document_review`, `low_confidence_background`,
  `red_mark_review`

## Comparison With Stroke-Protected Background Path

Reference: `ocr-preprocess-stroke-bg-v1` on the same 12-image real sample set.

- Stroke-bg average `ocr_text_edge_energy_ratio`: 0.913637
- Structure path explicit-grayscale average `ocr_text_edge_energy_ratio`:
  0.942257
- Stroke-bg average `ocr_binary_foreground_retention_ratio`: 0.997369
- Structure path explicit-grayscale average `ocr_binary_foreground_retention_ratio`:
  0.996808
- Both paths kept foreground retention effectively at 1.0 and had zero
  guardrail failures.

Interpretation: the structure-preserving route retains more edge energy than
the stroke-bg route, while keeping foreground loss near zero. It is a better
candidate for text/table preservation, but it still does not prove net clarity
improvement over the original image because average edge energy remains below
the source baseline of 1.0.

## Visual Assessment

Manual review of the contact sheet found:

- Main grayscale outputs did not show the earlier water-ripple artifact on the
  sparse handwriting sample.
- Table/grid lines stayed visually straight and continuous in the main output.
- The binary sidecar preserved table structure more strongly than prior
  grayscale-only paths.
- The enhancement remains conservative: it improves background separation and
  OCR binary readiness more than it improves visual sharpness of the main
  derivative.

## Current Verdict

Keep `ocr-preprocess-structure-v1` as an experimental parallel OCR path for
further comparison. It is stronger than `ocr-preprocess-stroke-bg-v1` on
structure preservation metrics, but it is not yet a promoted default OCR route.

Next validation should add OCR-engine measurements, such as CER/WER or OCR
confidence, and table-line continuity metrics on the private real-sample set.
