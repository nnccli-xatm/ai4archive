# Standards Traceability

This note maps the current `archive-scan-qc` rule system to public DA/T 31-2017
reference points. It uses clause short names and implementation summaries only;
it does not reproduce the standard text.

Public references:

- 国家档案局发布 DA/T 31-2017《纸质档案数字化规范》的通知: https://www.saac.gov.cn/daj/tzgg/201709/17b7764728de403fb1c0f0085de6fa61.shtml
- 全国标准信息公共服务平台行业标准《纸质档案数字化规范》: https://std.samr.gov.cn/hb/search/stdHBDetailed?id=8B1827F24605BB19E05397BE0A0AB44A
- 国家数字标准馆 DA/T 31-2017 条目: https://www.ndls.org.cn/standard/detail/701c1aa6791563e848548a7f7199e355

## Mapping Table

| DA/T 31-2017 clause | Short requirement summary | Current rules and fields | Implementation status | Gaps and next plan |
| --- | --- | --- | --- | --- |
| DA/T 31-2017 9.5 scan parameters and image format | Scanning output should carry controlled resolution, image format, and related capture settings that can be checked during delivery. | `dpi_minimum`, `dpi_missing`, `unsupported_format`, `batch_dpi_consistency`, `batch_color_mode_consistency`, `batch_format_consistency`; report fields include `dpi_x`, `dpi_y`, `format`, `color_mode`, and active rules profile thresholds. | Automated extraction from image metadata and aggregate consistency checks. Preflight validates the rules profile before a run. | Capture scanner/operator metadata when a trusted ingest manifest provides it. |
| DA/T 31-2017 10.2 rotation and deskew | Pages that require rotation or skew correction should be processed consistently and then reviewed. | `batch_orientation_consistency`, `quality_skew_candidate`; optional processing manifest records EXIF transpose and deskew decisions. | Automated orientation and conservative skew screening plus opt-in derivative deskew metadata. Findings are review prompts, not final manual acceptance decisions. | Add sampled human review instructions for orientation-heavy collections. |
| DA/T 31-2017 10.3 border cropping | Border cropping should be controlled so page information is not removed. | `quality_dark_border_candidate`; optional processing manifest records crop and dark-border trim decisions, output size, and source hash. | Automated conservative dark-edge border screening plus opt-in local derivative processing; original files are not modified. | Add operator approval workflow for derivative use in production packages. |
| DA/T 31-2017 10.4 decontaminate while preserving original appearance | Cleanup should improve obvious scan noise without hiding or changing the archive page's evidentiary appearance. | `quality_too_dark`, `quality_too_bright`, `quality_scanline_candidate`, `batch_color_mode_consistency`; optional processing manifest records despeckle and contrast-normalization decisions. | Partially automated. Metrics identify review candidates and derivative operations are recorded. | Add threshold calibration evidence to project SOPs. |
| DA/T 31-2017 10.5.1 complete and readable image | Images that are incomplete, unclear, or distorted should be identified for rescan or review. | `openability`, `dimensions`, `quality_too_dark`, `quality_too_bright`, `quality_low_contrast`, `quality_suspected_blur`, `quality_near_blank_page`, `quality_scanline_candidate`; file fields include dimensions, brightness, contrast, sharpness, foreground coverage, edge coverage, and scanline reason metrics. | Automated metadata checks plus conservative screening metrics. | Add project-calibrated thresholds by collection type and scanner model after internal sample review. |
| DA/T 31-2017 10.5.2 missed/rescanned/extra scans | Missing pages, repeated pages, and extra scans should be corrected promptly. | `duplicate_file`, `manifest_missing_file`, `manifest_unexpected_file`, `quality_near_blank_page`, `multi_page_image_container`; manifest summary counts, finding counts, and `frame_count` where Pillow exposes image-container pages/frames. | Implemented when a `relative_path` manifest CSV is supplied; duplicate content checks run without a manifest. Multi-page image containers are flagged for project policy review rather than platform adaptation. | Add richer catalog field validation when production catalog exports are available. |
| DA/T 31-2017 10.5.3 page order consistency | Digital image order should remain aligned with the source archive order and manifest sequence. | `duplicate_name`, `manifest_duplicate_entry`, `manifest_invalid_sequence`, `manifest_duplicate_sequence`, `manifest_sequence_gap`, `manifest_order_mismatch`, `name_pattern`; manifest sequence counts and row-level findings. | Automated filename/profile checks plus optional manifest page/order validation for `sequence`, `page_sequence`, `page_number`, or `expected_order`. Strict contiguous sequence checks run when the manifest declares `strict_sequence`, `sequence_strict`, or `strict_page_sequence`. | Add richer production catalog field validation when catalog exports provide stable page identifiers beyond file paths and numeric order. |
| DA/T 31-2017 10.5.4 processing quality inspection | Processing results such as stitching, rotation/deskew, cropping, and cleanup should be checked. | `batch_orientation_consistency`, `quality_skew_candidate`, `quality_dark_border_candidate`; `processing_manifest.json` records transpose, crop, deskew, border trim, despeckle, and output size. | Implemented as automated screening plus derivative operation evidence. | Add stitch-specific rules if multi-image stitching enters scope. |
| DA/T 31-2017 11.2 catalog-image correspondence | Catalog data and image files should correspond, counts should agree, page order should agree, and images should open normally. | `openability`, `manifest_missing_file`, `manifest_unexpected_file`, `manifest_duplicate_entry`, `manifest_invalid_sequence`, `manifest_duplicate_sequence`, `manifest_order_mismatch`, `duplicate_name`, `name_pattern`; manifest summary counts and row-level findings. | Implemented when a `relative_path` manifest CSV is supplied; sequence checks activate only when a supported optional sequence column is present. Openability always runs. | Add production catalog export validation once field contracts are available. |
| DA/T 31-2017 12.1.2 automated plus manual inspection | Computer checks and manual review should be combined for quality inspection. | JSON/HTML/CSV reports, `rule_catalog`, quality metrics, severity counts, and review-oriented explanations. | Implemented. Automated results explain what to review; subjective acceptance remains an operator decision. | Add sampled manual-review logs outside this package. |
| DA/T 31-2017 12.2 acceptance scope | Acceptance artifacts should make quality decisions, catalog correspondence, and delivery evidence reviewable. | `scan_qc_report.json`, standalone HTML, CSV exports, `preflight_report.json`, `benchmark_results.json`, `processing_manifest.json`, performance metadata, rule profile metadata, `frame_count`, and `rule_catalog`. | Implemented. JSON/HTML expose the active rule catalog and the rules profile used for the run. | Add signed release bundles or checksums if required by the deployment authority. |
| Privacy and controlled evidence sharing | Public evidence should use aggregate or static metadata instead of file-level sensitive data. This is an implementation control around delivery evidence, not a quoted DA/T clause. | Benchmark JSON/CSV are aggregate-only. Rule catalog contains static metadata only. Row-level scan reports remain sensitive because they include filenames, paths, hashes, and per-file metrics. | Implemented privacy boundary: catalog and benchmark outputs avoid file content, filenames, paths, hashes, and thumbnails. | Add deployment-specific retention and redaction rules outside the package code. |

## Rule Registry Contract

`src/archive_scan_qc/rule_registry.py` is the source of truth for rule metadata.
Each finding rule has:

- `rule_id`
- title
- default severity
- DA/T 31-2017 clause-numbered short references such as `DA/T 31-2017 10.5.1`
- check target
- automation status
- report explanation

The scanner embeds `rule_catalog` in `scan_qc_report.json`; the HTML report
renders the same catalog in a Rule Catalog section. The catalog is static
aggregate metadata and must not include filenames, relative paths, absolute
paths, hashes, thumbnails, OCR text, or image content.

## Validation Hooks

- Preflight validates configuration, rules profile loading, worker settings, and
  manifest shape before image scanning.
- Unit tests require the registry to cover all current finding rules produced by
  scanner tests.
- Unit tests require JSON/HTML reports to include rule catalog data.
- Unit tests require rule catalog text to remain privacy-safe static metadata.
- `scripts/validate_release.py` runs the unit tests, examples-based preflight,
  full scan/process dry-run, benchmark validation, compileall, wheel build, and
  install smoke test.

## Manifest Page Sequence Validation

Manifest validation remains backward compatible: a CSV with only
`relative_path` continues to work. When a manifest also includes `sequence`,
`page_sequence`, `page_number`, or `expected_order`, the scanner records the
recognized field name and aggregate sequence counts in JSON, HTML, CSV, and
run-plan summaries.

Sequence values must be positive integers. Duplicate values produce protected
P0 `manifest_duplicate_sequence` findings because two catalog rows cannot
represent the same declared page order. Invalid values produce P1
`manifest_invalid_sequence` findings because order evidence is present but not
machine-checkable. If the manifest declares strict sequence mode with
`strict_sequence`, `sequence_strict`, or `strict_page_sequence`, missing
integers inside the observed range produce P1 `manifest_sequence_gap` evidence.
When manifest row order differs from the deterministic discovered/report file
order for the same files, P2 `manifest_order_mismatch` findings flag the batch
for operator review.

This maps DA/T 31-2017 page-order and catalog-image correspondence expectations
to conservative local checks. The tool validates the order evidence available
in the manifest; it does not infer unprovided catalog metadata or expose
private catalog rows in aggregate preflight and run-plan outputs.
