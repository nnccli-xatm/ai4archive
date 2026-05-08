# Standards Traceability

This note maps the current `archive-scan-qc` rule system to public DA/T 31-2017
reference points. It uses clause short names and implementation summaries only;
it does not reproduce the standard text.

Public references:

- 国家档案局发布 DA/T 31-2017《纸质档案数字化规范》的通知: https://www.saac.gov.cn/daj/tzgg/201709/17b7764728de403fb1c0f0085de6fa61.shtml
- 全国标准信息公共服务平台行业标准《纸质档案数字化规范》: https://std.samr.gov.cn/hb/search/stdHBDetailed?id=8B1827F24605BB19E05397BE0A0AB44A
- 国家数字标准馆 DA/T 31-2017 条目: https://www.ndls.org.cn/standard/detail/701c1aa6791563e848548a7f7199e355

## Mapping Table

| DA/T 31-2017 area | Short requirement summary | Current rules and fields | Implementation status | Gaps and next plan |
| --- | --- | --- | --- | --- |
| Digital image quality | Digital images should be readable, complete enough for review, and suitable for acceptance checks. | `openability`, `dimensions`, `quality_too_dark`, `quality_too_bright`, `quality_low_contrast`, `quality_suspected_blur`, `quality_near_blank_page`, `batch_orientation_consistency`; file fields include dimensions, color mode, orientation, brightness, contrast, sharpness, foreground coverage, edge coverage. | Automated metadata checks plus conservative screening metrics. Findings are explanations, not final manual acceptance decisions. | Add project-calibrated thresholds by collection type and scanner model after internal sample review. |
| Scanning parameters | Scan settings such as resolution and color treatment need to be controlled and evidenced. | `dpi_minimum`, `dpi_missing`, `batch_dpi_consistency`, `batch_color_mode_consistency`, `batch_format_consistency`; report fields include `dpi_x`, `dpi_y`, `format`, `color_mode`, active rules profile thresholds. | Automated extraction from image metadata and aggregate consistency checks. | Capture scanner/operator metadata when a trusted ingest manifest provides it. |
| Image processing | Processing should preserve evidentiary value and avoid changing source scans unexpectedly. | Optional processing manifest records EXIF transpose, crop, deskew, dark-border trim, despeckle, output size, source hash, and operation decisions. Reports note that source images are read-only. | Implemented as opt-in local derivative processing; original files are not modified. | Add operator approval workflow for derivative use in production packages. |
| Acceptance and delivery evidence | Acceptance artifacts should make quality decisions reviewable and repeatable. | `scan_qc_report.json`, standalone HTML, CSV exports, `preflight_report.json`, `benchmark_results.json`, `processing_manifest.json`, performance metadata, rule profile metadata, and `rule_catalog`. | Implemented. JSON/HTML expose the active rule catalog and the rules profile used for the run. | Add signed release bundles or checksums if required by the deployment authority. |
| Catalog-image correspondence | File inventory and directory contents should match the expected catalog or manifest. | `manifest_missing_file`, `manifest_unexpected_file`, `manifest_duplicate_entry`, `duplicate_file`, `duplicate_name`, `name_pattern`; manifest summary counts and row-level findings. | Implemented when a `relative_path` manifest CSV is supplied; duplicate content and name checks run without a manifest. | Add richer catalog field validation when production catalog exports are available. |
| Keep original appearance | Digitization and derivative operations should avoid hiding or distorting original page information. | Conservative quality rules flag darkness, brightness, low contrast, blur, near blank pages, mixed orientation, and mixed color mode. Processing defaults leave source files untouched and records every derivative decision. | Partially automated. Metrics identify review candidates; manual confirmation remains required for subjective appearance issues. | Add sampled human review instructions and threshold calibration evidence to project SOPs. |
| Privacy and controlled evidence sharing | Evidence exports should separate public aggregate validation from sensitive row-level file data. | Benchmark JSON/CSV are aggregate-only. Rule catalog contains static metadata only. Row-level scan reports remain sensitive because they include filenames, paths, hashes, and per-file metrics. | Implemented privacy boundary: catalog and benchmark outputs avoid file content, filenames, paths, hashes, and thumbnails. | Add deployment-specific retention and redaction rules outside the package code. |

## Rule Registry Contract

`src/archive_scan_qc/rule_registry.py` is the source of truth for rule metadata.
Each finding rule has:

- `rule_id`
- title
- default severity
- DA/T 31-2017 short standard references
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

