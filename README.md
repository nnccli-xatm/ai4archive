# ai4archive

AI tools for archive workflows.

## Scan QC phase-one CLI

This repository now includes a minimal local CLI for the first implementation
phase described in `archive-scan-qc-retouch-design.md`: standard rules and a
batch-processing skeleton for scanned image quality control.

The current implementation covers:

- project and batch identifiers for an imported image directory
- recursive image-directory import without modifying source files
- file openability checks
- image format, DPI, color mode, width, height, file size, and SHA-256 capture
- Pillow-only image quality metrics: grayscale brightness mean, grayscale
  contrast standard deviation, Laplacian-variance sharpness approximation,
  dark-pixel ratio, foreground coverage, and edge coverage
- duplicate filename and duplicate file-content checks
- conservative P1/P2 quality findings for over-dark, over-bright,
  low-contrast, suspected-blur, and near-blank pages
- batch-level format, DPI, and color-mode consistency findings
- optional batch manifest CSV consistency checks
- JSON report, standalone HTML report, and CSV file and finding exports
- optional local derivative processing with conservative auto-crop and deskew

### Dependency choice

The CLI uses Pillow for local image openability and metadata extraction. Pillow
is open source, lightweight, cross-platform, and works offline. Hashing, JSON,
CSV, path handling, and rule checks use only the Python standard library. This
keeps phase one aligned with the design constraints: no cloud services, no
large frontend framework, no generative image repair, and no original-image
overwrites. The quality metrics, crop, and deskew implementations are also
Pillow-only; they do not add OpenCV, scikit-image, GPU, cloud, or other heavy
native dependencies.

### Production install

Use Python 3.10, 3.11, or 3.12 in production. The package exposes one console
script, `archive-scan-qc`, and keeps the runtime dependency set intentionally
small: Pillow plus the Python standard library.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install .
archive-scan-qc --version
```

For local development, use `python -m pip install -e .` inside the same kind of
virtual environment.

For offline or domestic-platform deployments, build or mirror wheels on a
networked machine that matches the target Python and platform, then install
from a local wheelhouse:

```bash
python -m pip wheel --wheel-dir wheelhouse .
python -m pip download --only-binary=:all: --dest wheelhouse 'Pillow>=10,<13'
python -m pip install --no-index --find-links wheelhouse ai4archive
```

If the target platform cannot use a prebuilt Pillow wheel, prepare the platform
toolchain and image libraries required by Pillow before installation, or build a
validated internal Pillow wheel and install only from that trusted wheelhouse.

### Run

```bash
archive-scan-qc preflight \
  --input /path/to/scanned-images \
  --out /path/to/qc-report \
  --process-out /path/to/processed-images \
  --auto-crop \
  --deskew \
  --workers 2 \
  --project demo-project \
  --batch batch-001 \
  --manifest-csv /path/to/manifest.csv \
  --rules-profile /path/to/rules.json

archive-scan-qc \
  --input /path/to/scanned-images \
  --out /path/to/qc-report \
  --process-out /path/to/processed-images \
  --auto-crop \
  --deskew \
  --workers 2 \
  --project demo-project \
  --batch batch-001 \
  --min-dpi 200 \
  --name-pattern 'A001_\d{4}' \
  --manifest-csv /path/to/manifest.csv \
  --rules-profile /path/to/rules.json
```

The optional manifest CSV must include a `relative_path` column whose values
are expected image paths relative to `--input`. Manifests may also include one
page/order column named `sequence`, `page_sequence`, `page_number`, or
`expected_order`. These values are treated as positive integers and are used to
check duplicate page sequence values, invalid values, and practical order
mismatches between manifest row order and the deterministic discovered file
order. Add `strict_sequence`, `sequence_strict`, or `strict_page_sequence` with
`true`/`1`/`yes` when a project requires contiguous sequence values; preflight
then reports gaps as blocking aggregate errors.

For project-scale production runs, use a local run plan to drive multiple
batches through preflight, scan, and optional processing:

```bash
archive-scan-qc run-plan \
  --plan-csv /path/to/run-plan.csv \
  --out /path/to/project-qc-output \
  --project project-code \
  --continue-on-error
```

The plan may be CSV or JSON. Each batch row/object must include `batch_id` and
`input_dir`, and may include `report_dir` or a relative report name,
`process_out`, `manifest_csv`, `rules_profile`, `workers`, `min_dpi`,
`name_pattern`, `auto_crop`, `deskew`, `trim_dark_border`, `despeckle`, and
`resume_processing`. Relative `input_dir`, `manifest_csv`, and `rules_profile`
values are resolved relative to the plan file. Relative `report_dir` and
`process_out` values are resolved under `--out`.

`run-plan` writes each batch's normal local artifacts in its batch report and
processing directories, then writes project-level `run_plan_summary.json` and
`run_plan_summary.csv` under `--out`. The project summary is aggregate-only:
it includes batch counts, pass/fail counts, P0/P1/P2 totals, manifest sequence
validation totals, processing failure totals, preflight error totals, throughput
aggregates, and failed batch IDs. It
does not include source filenames, relative or absolute source paths, hashes,
thumbnails, row-level file metadata, or image content. Batch-level reports and
processing manifests keep their existing sensitive local evidence behavior.

By default, `run-plan` stops at the first failed batch and exits non-zero. Add
`--continue-on-error` to keep running later batches while still recording the
failed batch in the project summary and returning non-zero at the end.

### Local private sample integration

Use `scripts/run_private_integration.py` only inside the internal/local
environment that can access private sample images. The repository does not
include private images, private paths, private filenames, hashes, thumbnails,
or row-level private results.

```bash
PYTHONPATH=src python3 scripts/run_private_integration.py \
  --input /placeholder/private-image-directory \
  --out /placeholder/private-output-root \
  --workers 4 \
  --process-images \
  --auto-crop \
  --deskew
```

The same required paths may be supplied through environment variables:

```bash
PRIVATE_INTEGRATION_INPUT=/placeholder/private-image-directory \
PRIVATE_INTEGRATION_OUT=/placeholder/private-output-root \
PYTHONPATH=src python3 scripts/run_private_integration.py \
  --workers 4 \
  --process-images
```

The script runs preflight, scan, optional derivative processing, run-plan, and
an aggregate benchmark. The private integration summary uses the main run-plan
as the source of `aggregate_counts`; repeated benchmark worker runs are reported
only under benchmark-scoped fields so their per-run finding totals are not
confused with the main batch finding count. Benchmark throughput fields use the
best recommendation observed in the benchmark summary, not simply the first run.
The acceptance field is generated with the same aggregate-only
`archive_scan_qc.acceptance` logic used by `archive-scan-qc acceptance-summary`;
when optional review or processing-audit evidence is absent, the summary keeps
the acceptance warning instead of inventing row-level evidence.

Only stdout and `/placeholder/private-output-root/private_integration_summary.json`
are intended as aggregate-only public outputs. They include total files,
openable files, finding counts, processing counts, throughput, failed batch
count, benchmark repeated-run aggregates, acceptance status, and the output
directory name. The script performs a redaction self-check on the public summary
and fails if it finds sensitive keys or private path values such as source
paths, `relative_path`, `filename`, `sha256`, `thumbnail`, or `reviewer_notes`.

Normal scan reports, processing manifests, retry manifests, and any row-level
review artifacts remain under `/placeholder/private-output-root` as sensitive
local evidence. Do not upload them to public systems.

### Aggregate performance baseline runner

For the P0 aggregate baseline on `puersai-hpc`, use the dedicated wrapper around
the private integration path:

```bash
PYTHONPATH=src python3 scripts/run_aggregate_baseline.py \
  --input /placeholder/private-image-directory \
  --out /placeholder/private-output-root \
  --workers 4 \
  --benchmark-workers-list 1,2,4,8 \
  --process-images \
  --cleanup-artifacts
```

The same paths and worker settings may be supplied without putting private paths
in shell history:

```bash
PUERSAI_HPC_BASELINE_INPUT=/placeholder/private-image-directory \
PUERSAI_HPC_BASELINE_OUT=/placeholder/private-output-root \
PUERSAI_HPC_BASELINE_WORKERS=4 \
PUERSAI_HPC_BASELINE_WORKERS_LIST=1,2,4,8 \
PUERSAI_HPC_BASELINE_CLEANUP_ARTIFACTS=1 \
PYTHONPATH=src python3 scripts/run_aggregate_baseline.py
```

The runner writes
`/placeholder/private-output-root/aggregate_baseline_summary.json` with schema
`scan-qc.aggregate-baseline.v1`. The JSON contains only:

- `schema_version`, `generated_at`, and a non-sensitive `target_environment`
  label; `gpu_acceleration_used` is currently `false` because the existing
  CPU/Pillow path is unchanged.
- `worker_settings` and `operations`.
- `aggregate_counts`: total/openable files, P0/P1/P2 findings, processing
  processed/failed counts, failed batches, and preflight error count.
- `stage_timings`: unchanged scan and processing elapsed seconds, scan
  files/minute, scan openable files/minute, processing processed files/minute,
  aggregate per-operation processing timings for enabled operations such as
  auto-crop, deskew, dark-border trim, and despeckle, and benchmark rates when
  benchmark evidence is available. Optional aggregate wrapper timings also
  include `run_plan_and_benchmark.elapsed_seconds`,
  `report_write.elapsed_seconds`, and `total_wall_clock.elapsed_seconds`.
- `benchmark`: aggregate repeated-run count, rule-count totals, and
  `worker_sweep` evidence. The sweep lists each requested worker count with
  mean scan files/minute, mean processing files/minute when processing is
  enabled, processing failure count, whether operation timing evidence is
  present, and a conservative recommended worker count.
- `environment`: Python, platform, machine, processor, CPU count, memory, GPU
  placeholder, and executable name.
- `cleanup`: whether generated private artifacts were removed, cleanup
  `elapsed_seconds`, and which known output directories/files were deleted or
  preserved.
- `privacy_self_check`: pass/fail status and aggregate violation count.

Privacy guarantees: `aggregate_baseline_summary.json` omits source names, source
paths, relative paths, content hashes, thumbnails, OCR text, image content, and
row-level findings. When `--cleanup-artifacts` or
`PUERSAI_HPC_BASELINE_CLEANUP_ARTIFACTS=1` is enabled, the runner preserves
`aggregate_baseline_summary.json` for reporting and removes known generated
private artifacts under the output root, including `scan-reports`,
`processed-images`, `run-plan`, `benchmark`, `benchmark-processed`, and
`private_integration_summary.json`. The cleanup guard preserves the private
input image directory if it overlaps the output tree and never targets source
code workspaces. Without cleanup, sensitive scan reports, processing manifests,
derivative images, retry manifests, and normal private integration outputs
remain local under the private output root. Only aggregate results from
`aggregate_baseline_summary.json` should be reported externally; do not upload
private artifacts to PRs, Linear comments, logs, screenshots, or row-level
reports.

For the fixed 20-image private orchestrator validation on `puersai-hpc`, run the
same command against that sample with `--cleanup-artifacts`. After the command
returns, the private output root should retain only
`aggregate_baseline_summary.json`; the summary's `cleanup.removed_artifacts`
field records the removed generated report/image directories. The worker sweep
recommendation selects the lowest worker count within 90% of the best observed
aggregate processing throughput when processing evidence has zero failures, or
scan throughput when processing is disabled. This keeps the recommendation
conservative instead of assuming `workers=4` or the highest-throughput setting
is always the best hardware balance.

### One-command production validation

For repeatable operator validation, use the production validation wrapper. It
runs the aggregate baseline with cleanup, then runs the aggregate acceptance gate
against the resulting `aggregate_baseline_summary.json`. The wrapper writes only
aggregate public evidence: `aggregate_baseline_summary.json` and
`acceptance_summary.json`.

Low-risk fixed 20-image validation:

```bash
PYTHONPATH=src python3 scripts/run_production_validation.py \
  --input /placeholder/private-20-image-sample \
  --out /placeholder/private-validation-output/20-image \
  --workers 4 \
  --benchmark-workers-list 1,2,4,8 \
  --process-images \
  --auto-crop \
  --deskew \
  --trim-dark-border \
  --despeckle \
  --min-scan-files-per-minute 100 \
  --min-processing-files-per-minute 60
```

Full fixed 149-image production validation:

```bash
PYTHONPATH=src python3 scripts/run_production_validation.py \
  --input /placeholder/private-149-image-sample \
  --out /placeholder/private-validation-output/full-149 \
  --workers 4 \
  --benchmark-workers-list 1,2,4,8 \
  --process-images \
  --auto-crop \
  --deskew \
  --trim-dark-border \
  --despeckle \
  --min-scan-files-per-minute 120 \
  --min-processing-files-per-minute 90
```

The command exits non-zero when the aggregate baseline privacy self-check fails,
cleanup does not retain only the aggregate baseline summary before acceptance,
processing failures are present, or the configured throughput thresholds are not
met. Stdout names only aggregate output files and aggregate counts/rates; do not
paste private input or output paths into public reports.

Current `puersai-hpc` production baseline after PR #61 uses the full fixed
private sample of 149 files. The latest aggregate-only result is:

- scan: 64.815357 seconds, 137.93 files/minute
- processing: 80.103532 seconds, 111.61 files/minute
- totals: 149 total files, 149 openable files, 149 processed files
- processing failures: 0
- findings: 24 total, P0=22, P1=0, P2=2
- privacy self-check: pass
- cleanup: generated private images and reports removed, with only
  `aggregate_baseline_summary.json` retained as public aggregate evidence

Latest aggregate operation timings are `auto_crop` 4.719535 seconds, `deskew`
122.636509 seconds, `trim_dark_border` 1.202172 seconds, and `despeckle`
108.368089 seconds. These timings make `deskew` and `despeckle` the next
primary optimization targets; `auto_crop` and `trim_dark_border` are not the
current bottlenecks on this sample.

Validation policy: small low-risk changes may use the fixed 20-image private
sample. Core image-processing, quality-rule, privacy-boundary, and release
changes require the full 149-image private sample. Documentation-only changes
can be validated by documentation diff review without running private images.

Run `archive-scan-qc preflight` before production batches. It validates the
input directory, output and processing-output configuration, worker count, rules
profile loading, and manifest structure without opening, copying, modifying, or
deriving images. It writes `preflight_report.json` under `--out` and prints a
short stdout summary. The report is aggregate-only: it records counts and config
state, not per-file path lists, hashes, thumbnails, or image content.

Preflight exits `0` when all required checks pass. It exits non-zero for errors
that would block or fail a production run, including missing input directories,
invalid output paths, processing flags without `--process-out`, invalid workers,
invalid rules profiles, unsafe manifest paths, duplicate manifest entries, or
manifest missing/unexpected file counts. Warnings identify risky but runnable
configuration, such as outputs placed under the input tree.

Use `--workers N` to control local scan and derivative-processing concurrency.
`--workers 1` forces fully serial mode for comparison baselines. When omitted,
the CLI uses a conservative default and caps the effective worker count by CPU
count, batch size, and an internal safety ceiling to avoid filling memory on
low-spec or domestic-platform deployments. The effective worker count and mode
are printed on stdout and recorded in JSON report and processing manifest
performance metadata.

The optional `--rules-profile` argument loads a local JSON rules profile. When
omitted, the built-in default profile preserves the current behavior:
`min_dpi=200`, no filename pattern, quality thresholds of dark `<45`, bright
`>250` with contrast `<10`, low contrast `<10`, suspected blur when contrast is
at least `12` and Laplacian variance is below `20`, and the existing P0/P1/P2
severities. Explicit CLI `--min-dpi` and `--name-pattern` values override the
loaded profile for that run.

Example rules profile:

```json
{
  "name": "project-archive-standard",
  "version": "2026.1",
  "min_dpi": 300,
  "name_pattern": "A001_\\d{4}",
  "quality_thresholds": {
    "dark_mean_threshold": 40,
    "bright_mean_threshold": 248,
    "low_contrast_stddev_threshold": 12,
    "blur_laplacian_variance_threshold": 25,
    "blur_min_contrast_stddev": 14,
    "blank_brightness_min": 248,
    "blank_contrast_max": 6,
    "blank_foreground_coverage_max": 0.003,
    "blank_edge_coverage_max": 0.002,
    "blank_dark_pixel_ratio_max": 0.0005
  },
  "rules": {
    "quality_too_dark": {
      "enabled": true,
      "severity": "P1"
    },
    "quality_suspected_blur": {
      "enabled": false
    },
    "name_pattern": {
      "severity": "P2"
    }
  }
}
```

Configurable top-level fields are `name`, `version`, `min_dpi`, and
`name_pattern`. `quality_thresholds` accepts
`dark_mean_threshold`, `bright_mean_threshold`,
`low_contrast_stddev_threshold`, `blur_laplacian_variance_threshold`,
`blur_min_contrast_stddev`, `blank_brightness_min`, `blank_contrast_max`,
`blank_foreground_coverage_max`, `blank_edge_coverage_max`, and
`blank_dark_pixel_ratio_max`. `rules` is keyed by finding rule name; each rule may
set `enabled` to `true` or `false` and may override `severity` to `P0`, `P1`, or
`P2`. Critical integrity P0 rules such as `openability`,
`manifest_missing_file`, manifest unexpected/duplicate checks, duplicate file
checks, missing dimensions, and `dpi_minimum` cannot be disabled or downgraded
by profile settings.

The profile layer is intended for aligning a batch with a national, industry,
or project-specific archive digitization standard without editing code. Keep
the profile file under local change control with the standard name/version, and
encode the agreed DPI, filename, brightness, contrast, and blur thresholds
there. Invalid profile paths, malformed JSON, or wrong field types stop the CLI
before reports are written.

### Optional offline analysis provider

By default no model or external analyzer runs; the CLI uses only built-in rules.
To reserve a production-grade local model hook without adding GPU, network, or
large-model dependencies, pass an optional local command:

```bash
archive-scan-qc \
  --input /path/to/scanned-images \
  --out /path/to/qc-report \
  --project demo-project \
  --batch batch-001 \
  --analysis-provider-command '/approved-tools/local-provider --profile safe'
```

For a deterministic contract example that uses only the Python standard
library, run:

```bash
archive-scan-qc \
  --input /path/to/synthetic-images \
  --out /path/to/qc-report \
  --analysis-provider-command 'python3 examples/local_analysis_provider.py'
```

The provider runs as a controlled local child process. The scanner sends JSONL
on stdin with minimized fields: project/batch identifiers, input/output
directories, source `relative_path` and absolute local path, openability and
basic image metadata, and rules profile metadata. It does not send image bytes,
thumbnails, OCR text, hashes, or file content. The provider must not upload
images or derived content; run it only in the same approved local environment
as the source scans. Omit `--analysis-provider-command` to disable the provider.

Provider output is JSONL on stdout. Metadata records are optional:

```json
{"type":"metadata","provider":{"name":"local-qc-model","version":"2026.1","model":"offline-small"}}
```

Finding records must use the provider namespace and include confidence:

```json
{"type":"finding","relative_path":"A001_0001.png","rule":"provider.local-qc-model.blur","severity":"P2","confidence":0.82,"message":"Local provider blur signal.","metadata":{"model":"offline-small"}}
```

Provider rule ids must match `provider.<name>.<rule>` and cannot override
built-in rules such as `openability`, `dpi_minimum`, duplicate checks, or
manifest P0 checks. Invalid JSONL, unknown paths, invalid severities,
out-of-range confidence values, or non-provider rule ids stop the scan with a
clear error before reports are written. Accepted provider findings are merged
with built-in findings and marked with `source: "provider"`; built-in rule
findings are marked with `source: "rules"`.

Reports include provider aggregate metadata and provider finding counts. They
still must not contain embedded images, thumbnails, OCR text, or file content;
provider metadata keys that look like OCR/text/content/image/path/hash payloads
are dropped before serialization.

Future PaddleOCR, ONNX, Paddle, or other model providers should keep the same
JSONL contract at the process boundary. If GPU acceleration is needed, wrap the
model in a local sidecar executable or service that is launched by the operator
inside the approved workstation or secured processing node, then point
`--analysis-provider-command` at a small local client. The sidecar may open the
local file path it receives, but it must not send source images, thumbnails,
OCR text, embeddings, model prompts, filenames, hashes, or derived content to a
cloud service or external network. Provider stdout must contain only metadata
and findings in the documented schema; do not emit OCR transcripts, detected
personal data, image crops, base64 payloads, absolute paths, raw model logs, or
private filenames.

### Review and rule calibration

After a local scan, treat `scan_qc_report.json`, HTML, CSVs, and review
templates as sensitive evidence because they include row-level paths, hashes,
messages, and reviewer notes. Export a local template, complete manual review,
then write an aggregate review summary:

```bash
archive-scan-qc rework-action-list \
  --report /path/to/qc-report/scan_qc_report.json \
  --processing-audit-summary /path/to/processed/processing_audit_summary.json \
  --processing-retry-manifest /path/to/processed/processing_retry_manifest.json \
  --out /path/to/qc-report/rework_action_list.json \
  --csv-out /path/to/qc-report/rework_action_list.csv

archive-scan-qc review-export \
  --report /path/to/qc-report/scan_qc_report.json \
  --out /path/to/qc-report/review_template.csv

archive-scan-qc review-summary \
  --review /path/to/qc-report/review_template.csv \
  --out /path/to/qc-report/review_summary.json
```

`rework_action_list.json` and optional CSV are local-only sensitive operator
work queues. They group row-level QC findings and processing retry evidence
into practical actions: rescan required, reprocess candidate, manual review,
duplicate/manifest correction, processing retry, and informational follow-up.
They deliberately contain row-level paths, hashes, messages, and processing
errors for local remediation, but never embed thumbnails, image bytes, or source
images. Use this artifact after automated QC and derivative-processing evidence
exist, and before completing the review template, `review_summary.json`, or the
final `acceptance_summary.json`.

For DA/T 31-2017-style manual sampling acceptance, export a deterministic
local sampling list from the scan report:

```bash
archive-scan-qc acceptance-sampling-export \
  --report /path/to/qc-report/scan_qc_report.json \
  --out /path/to/qc-report/acceptance-sampling
```

This writes `acceptance_sampling_review.json` and
`acceptance_sampling_review.csv`. The default sample ratio is 5% and cannot be
lowered; P0/P1/P2 findings and other problematic records are selected first
where practical, then remaining records are filled by stable deterministic
ordering. The artifacts are sensitive local evidence because they include
row-level paths and hashes for operator inspection. They do not embed images,
thumbnails, OCR text, or image bytes. The JSON also includes aggregate sampling
counts that can be copied into acceptance notes or future aggregate acceptance
integration after local policy review.

Use aggregate calibration only after automated QC and review:

```bash
archive-scan-qc calibrate-rules \
  --report /path/to/qc-report/scan_qc_report.json \
  --review-summary /path/to/qc-report/review_summary.json \
  --out /path/to/qc-report/rules_calibration_summary.json \
  --write-suggested-profile /path/to/qc-report/rules-profile.suggested.json
```

`rules_calibration_summary.json` contains per-rule trigger counts, severity
distribution, optional review status distribution, conservative threshold
recommendations, and the current profile field summary. It deliberately omits
file paths, filenames, hashes, OCR text, image content, thumbnails, row-level
messages, and reviewer notes. The suggested profile output is draft-only and
does not overwrite the original profile; a human must approve any production
rules profile change.

For derivative processing review, create the local-only package from
`processing_manifest.json`:

```bash
archive-scan-qc processing-review-package \
  --manifest /path/to/processed/processing_manifest.json \
  --out /path/to/processed/local-review
```

This writes `processing_review_package.json` and a standalone
`processing_review_package.html` with grouped sections for deskewed,
dark-border-trimmed, cropped, despeckled, failed, and guardrail-warning records.
It may include row-level source and derivative relative paths, hashes, operation
decisions, failure reasons, and local derivative links. Treat it as sensitive
local evidence only. It is not an aggregate acceptance artifact and must not be
uploaded to public issues, PRs, chats, or release systems. The HTML uses safe
relative links for derivative outputs under the processing output tree and does
not embed base64 image data.

### Rule and standards traceability

The report includes a `rule_catalog` object and the standalone HTML report
includes a Rule Catalog section. This catalog explains each finding rule's
default severity, check target, automation status, DA/T 31-2017 clause-numbered
short reference such as `DA/T 31-2017 10.5.1`, and operator-facing meaning. It
is static metadata only; it must not contain
filenames, relative paths, absolute paths, hashes, thumbnails, OCR text, or
image content.

Use `docs/standards-traceability.md` to explain how specific DA/T 31-2017
clauses for scan parameters, rotation/deskew, cropping, cleanup with original
appearance preserved, image readability, missed or duplicate scans, page order,
catalog-image correspondence, automated plus manual inspection, and acceptance
scope map to current rules, report fields, preflight, benchmark output, and
release validation. Treat row-level JSON/HTML/CSV reports as sensitive, but the
rule catalog and standards mapping are suitable for public PR discussion because
they contain no private batch data.

The optional `--process-out` directory enables the first local image-processing
layer. Source images remain read-only. The CLI writes derivative images under
`--process-out/images`, preserving source relative paths, and writes
`--process-out/processing_manifest.json` to link each derivative back to the
source SHA-256. It also writes `processing_retry_manifest.json` for local retry
diagnosis and `processing_audit_summary.json` with aggregate-only counts,
flags, worker metadata, timing, throughput, failure totals, and resume counts.
The audit summary omits file lists, paths, hashes, thumbnails, and image
content so it can be used as the production batch audit artifact without
exposing private row-level data. The initial processing pipeline applies EXIF
orientation normalization, safe RGB/L color conversion, and light automatic
contrast normalization. Add `--auto-crop` with `--process-out` to enable
conservative Pillow-only page-border cropping for derivative images.

By default, rerunning the same command preserves the previous overwrite/rerun
semantics and processes every scanned record again. Add `--resume-processing`
to resume an interrupted derivative batch from an existing
`--process-out/processing_manifest.json`: files that were previously processed
successfully and still have their derivative under `--process-out/images` are
skipped, while previous failures, skipped records, and records with missing
derivatives are processed again. The new manifest records aggregate
`resumed_files`, `skipped_due_to_resume`, `reprocessed_files`, `failed_files`,
and `retry_list_files` counts, and stdout prints the same short resume/audit
summary.

Add `--deskew` with `--process-out` to enable conservative small-angle deskew
for derivative images. The processing layer estimates skew for every openable
image written to a processing manifest, even when `--deskew` is not enabled.
`skew_angle_degrees` uses Pillow's rotation direction convention: positive
values mean counterclockwise skew and negative values mean clockwise skew.
When correction is applied, the derivative is rotated by the opposite angle.
Automatic deskew is intentionally limited to high-confidence small angles, with
a default maximum absolute angle of 5 degrees. Blank pages, low-contrast pages,
low-confidence estimates, detection failures, very small angles, and angles
outside that threshold are not rotated; the manifest records `deskewed=false`
and a `deskew_reason`.

Add `--trim-dark-border` with `--process-out` to conservatively remove only
dark scan borders that touch the outer image edges. The detector limits trim
depth, enforces a minimum retained page ratio, and leaves low-confidence cases
unchanged so marginalia, stamps, body text, and edge annotations are not
cropped away. The manifest records `dark_border_trimmed`,
`dark_border_bbox`, and `dark_border_reason`.

Add `--despeckle` with `--process-out` to replace isolated dark speckles in
derivative images. This pass is intentionally small and Pillow-only: it targets
single-pixel dark noise surrounded by light background, and skips connected
dark neighborhoods to protect text strokes, ruled lines, boxes, and seals. The
manifest records `despeckled`, `despeckle_pixels_changed`, and
`despeckle_reason`.

When derivative operations are enabled together, the pipeline runs EXIF
transpose, color conversion, skew detection, conservative deskew, conservative
dark-border trim, conservative crop, isolated-pixel despeckle, and then light
autocontrast. Deskew runs before trimming/crop so the manifest has a clear
pre/post rotation size and edge operations can operate on the final page
orientation. Despeckle runs before autocontrast so small noise is removed
before tonal expansion. The processing manifest records each image's
`skew_angle_degrees`, `skew_confidence`, `deskewed`, `deskew_reason`,
`pre_deskew_size`, `post_deskew_size`, dark-border decision, crop bbox, crop
decision, despeckle decision, original size, output size, operations, and
failure reason. All retouching options are opt-in to avoid surprising source
batches; original images are still only read.

For repeatable runs, keep `--out` outside the scanned image tree when possible.
If `--out` is inside `--input`, the CLI automatically skips that output
directory and its children. It also skips the `--manifest-csv` file itself when
the manifest is stored under `--input`, hidden files, and hidden directories
such as `.git` or `.cache`.

The command writes:

- `scan_qc_report.json`
- `scan_qc_report.html`
- `scan_qc_files.csv`
- `scan_qc_findings.csv`

### Human review loop

After an automated scan, export findings into a reviewer-editable local
template:

```bash
archive-scan-qc review-export \
  --report /path/to/qc-report/scan_qc_report.json \
  --out /path/to/qc-report/review_template.csv
```

Use `.json` for `--out` to write the same template as JSON. The template fields
are stable: `finding_id`, `rule`, `severity`, `relative_path`, `status`, and
`reviewer_notes`. Reviewers should update `status` to one of `pending`,
`accepted`, `false_positive`, `fixed`, or `needs_rescan`, and may add notes for
local disposition tracking. The review template is sensitive local evidence
because it contains row-level paths and reviewer notes. Do not upload it to
public issues, PRs, chats, or release systems.

Create the aggregate acceptance summary from the filled template:

```bash
archive-scan-qc review-summary \
  --review /path/to/qc-report/review_template.csv \
  --out /path/to/qc-report/review_summary.json
```

`review_summary.json` contains only aggregate severity, rule, and status counts,
plus remaining P0/P1 counts and `acceptance_passed`. It does not include
filenames, paths, hashes, messages, or reviewer notes. P0/P1 findings are
considered remaining until their status is `fixed` or `false_positive`; the
acceptance threshold passes when remaining P0 and P1 counts are both zero.

The JSON report includes a batch `manifest` with project, batch, input
directory, output directory, rule version, generation time, total file count,
P0/P1/P2 finding counts, manifest usage/missing/unexpected/duplicate counts
when a manifest is provided, and `rules_profile` metadata. That metadata records
the profile source (`builtin` or the absolute JSON path), profile name/version,
the effective threshold summary, and per-rule settings for audit. Each openable
file record also includes:

- `quality_brightness_mean`: mean grayscale value on a 0-255 scale. Lower is
  darker; higher is brighter.
- `quality_contrast_stddev`: grayscale standard deviation on a 0-255 scale.
  Lower values indicate flatter, lower-contrast images.
- `quality_sharpness_laplacian_var`: variance of a simple 4-neighbor
  Laplacian approximation on a capped grayscale thumbnail. Lower values mean
  fewer hard edges and can indicate blur.
- `quality_dark_pixel_ratio`: share of thumbnail pixels at or below a dark
  grayscale threshold. Very low values mean almost no dark ink or marks.
- `quality_foreground_coverage`: share of thumbnail pixels at or below a
  conservative foreground threshold. Very low values indicate little visible
  page content.
- `quality_edge_coverage`: share of thumbnail pixels with enough local
  Laplacian response to look like content edges.
- `quality_skew_angle_degrees`, `quality_skew_confidence`, and
  `quality_skew_reason`: conservative scan-time skew estimate reused from the
  Pillow-only processing detector. Positive values follow Pillow's rotation
  convention; the scan report does not rotate the source image.
- `quality_dark_border_bbox` and `quality_dark_border_reason`: conservative
  scan-time dark-edge border candidate reused from the processing trim
  detector. The scan report does not crop the source image.
- `quality_scanline_orientation`, `quality_scanline_score`,
  `quality_scanline_location_ratio`, `quality_scanline_band_width`, and
  `quality_scanline_reason`: conservative scan-time row/column anomaly
  screening for obvious horizontal or vertical scanner lines and streaks.
- `quality_content_edge_cutoff_side`, `quality_content_edge_cutoff_score`,
  `quality_content_edge_cutoff_band_px`,
  `quality_content_edge_cutoff_dark_ratio`,
  `quality_content_edge_cutoff_span_ratio`, and
  `quality_content_edge_cutoff_reason`: conservative scan-time screening for
  localized dark content touching an image boundary, which can indicate
  over-cropped text, stamps, page numbers, or marginalia.
- `frame_count`: best-effort image container frame/page count when Pillow
  exposes it, such as for multi-page TIFF files.
- `orientation_class`: coarse page shape classification, one of `portrait`,
  `landscape`, or `square`; near-square images are treated as `square`.
- `aspect_ratio`: width divided by height, rounded for report display.
- `exif_orientation` and `exif_orientation_requires_transpose`: best-effort
  local EXIF orientation signal when Pillow can safely read it.

The default quality thresholds are intentionally conservative:

- `quality_too_dark` P1 when brightness mean is below `45`.
- `quality_too_bright` P1 when brightness mean is above `250` and contrast
  standard deviation is below `10`.
- `quality_low_contrast` P2 when contrast standard deviation is below `10`.
- `quality_suspected_blur` P2 when contrast standard deviation is at least
  `12` but Laplacian variance is below `20`.
- `quality_near_blank_page` P2 when a page is very bright, has contrast at or
  below `6`, foreground coverage at or below `0.003`, edge coverage at or
  below `0.002`, and dark-pixel ratio at or below `0.0005`.
- `quality_skew_candidate` P2 when the scan-time skew detector has a
  high-confidence small-angle estimate from `0.5` through `5` degrees.
- `quality_dark_border_candidate` P2 when the scan-time dark-border detector
  finds a conservative edge-touching trim candidate.
- `quality_scanline_candidate` P2 when a resized grayscale thumbnail has an
  obvious full-span horizontal or vertical intensity anomaly.
- `quality_content_edge_cutoff_candidate` P2 when localized dark content
  touches a narrow image-edge band without looking like a full dark border.
- `multi_page_image_container` P2 when Pillow reports more than one frame/page
  in an image container. This is a project policy review prompt for
  single-page delivery, not a platform adaptation step.
- `batch_orientation_consistency` P2 when a batch has a supported mix of
  portrait and landscape openable images after excluding square or near-square
  pages. The default is conservative and requires at least two files in each
  orientation class, so a single legitimate landscape attachment is not treated
  as a critical defect.

These thresholds are defaults in the built-in rules profile so batch-specific
callers can tune them through JSON without changing report structure. JSON
summary and manifest metadata include skipped file and directory counts for
auditability. The CSV file export includes the frame/page count and quality
metric columns. The HTML
report is a single static file with inline CSS for manual review; it shows the
batch manifest, summary counts, skipped counts, file metadata and quality metric
table, and finding table with P0/P1/P2 severity badges. It does not embed source
images, thumbnails, or file content.

Blank and near-blank findings are prompts for human review only. They are useful
for catching possible blank pages or missed scans, but they must not be used by
themselves as an automatic deletion rule because legitimate separator sheets,
backsides, faint stamps, page numbers, and very light annotations can look
nearly blank to aggregate metrics.

Orientation findings are also review prompts only. They help identify possible
rotated pages, portrait/landscape batches mixed by mistake, or EXIF orientation
metadata that needs attention, but they are not an automatic rotation decision.
Actual rotation or deskewing should continue to use `--deskew` or a later,
explicitly approved handling policy.

Skew and dark-border candidate findings are available in normal scan reports
without `--process-out`. They are processing-derived review prompts for pages
that may need deskew or border trim, not proof that a derivative operation is
approved. Actual deskewing and border trimming still require the explicit
processing flags and their audit manifests.

The process returns exit code `1` when P0 findings are present, so it can be
used in batch scripts. Reports are written to the output directory; original
images are only read.

### Privacy and production handling

Run private archive batches on local or approved internal machines only. Do not
upload source images, derivative images, normal scan QC reports, processing
manifests, filenames, path lists, thumbnails, hashes, or row-level finding data
from private collections to public systems. For issue comments, PR bodies, and
external benchmark notes, use synthetic samples or aggregate-only benchmark
outputs.

Keep `--out` and `--process-out` outside the source image tree when possible,
and store generated reports according to the collection's data classification.
The standalone HTML report has inline CSS and embedded JSON report data, but it
does not embed images or remote resources.

### Local benchmark metrics

For repeatable privacy-safe benchmarking across worker counts and hardware,
use the dedicated aggregate benchmark entry:

```bash
archive-scan-qc benchmark \
  --input /path/to/private-sample-images \
  --out /path/to/benchmark-output \
  --workers-list 1,2,4 \
  --repeats 3 \
  --deskew \
  --auto-crop \
  --trim-dark-border \
  --despeckle
```

Use `--scan-only` to measure scanning without derivative processing. When
processing is enabled, pass `--process-out /path/to/processed-benchmark-images`
if you want derivative images outside the benchmark output tree.

The benchmark writes only:

- `benchmark_results.json`
- `benchmark_results.csv`

These files are aggregate-only. They include total file count, openable count,
finding counts by severity and rule, processing success/failure/skip counts,
scan and processing elapsed seconds, aggregate per-operation processing timings,
files per minute, requested and effective workers, operation flags, Python
version, platform, CPU count, and best-effort memory/GPU fields. They do not
include source images, filenames, relative
paths, content hashes, thumbnails, per-file quality metrics, per-file finding
rows, single-file manifests, or image content.

`benchmark_results.json` also includes a top-level `recommendations` object for
capacity planning. It reports the best scan worker count, the best processing
worker count when processing was benchmarked, the corresponding mean files per
minute, effective workers, the selection basis, and diminishing-return notes
when an adjacent worker step improves throughput by less than the benchmark
threshold. The recommendation is based only on the aggregate data in the
current benchmark run; it does not assume a fixed CPU, memory, storage, GPU, or
private sample path. Keep `benchmark_results.csv` as run-level rows for
existing consumers.

For private local samples, share only `benchmark_results.json` and/or
`benchmark_results.csv`. Do not share the image directory, generated derivative
images, normal scan QC reports, processing manifests, filenames, thumbnails, or
row-level records from private collections. This makes it practical to compare
throughput on domestic platforms and different hardware while keeping sample
identity and content local.

After real-image validation, delete generated images and reports unless they
must stay inside the approved private evidence store. Public issue, PR, and
benchmark notes may retain only aggregate summaries; preserve the input test
data so the fixed 20-image and 149-image samples remain comparable across runs.

The regular scan CLI also prints concise timing lines after each run:

- `Scan elapsed`, `Scan files/min`, and `Scan openable files/min`
- `Scan workers`, showing the effective worker count and serial/parallel mode
- `Processing elapsed`, `Processing files/min`, and
  `Processing total files/min` when `--process-out` is used
- `Processing workers` when `--process-out` is used

The JSON scan report stores the same scan metrics under
`summary.performance` and `manifest.performance`: `started_at`, `finished_at`,
`elapsed_seconds`, `total_files`, `openable_files`, `files_per_minute`, and
`openable_files_per_minute`, plus `requested_workers`, `effective_workers`,
`worker_cap`, and `mode`. The processing manifest stores processing metrics
under both `performance` and `summary.performance`: `elapsed_seconds`,
`total_files`, `processed_files`, `skipped_files`, `failed_files`,
`processed_files_per_minute`, `total_files_per_minute`, `requested_workers`,
`effective_workers`, `worker_cap`, and `mode`.

Use these aggregate fields for local hardware baselines by running the same
batch and CLI options on each machine with `--workers 1`, `--workers 2`,
`--workers 4`, and the local CPU-count upper bound. Compare files per minute,
processed files per minute, elapsed seconds, worker mode, and effective worker
count. Use `recommendations.scan_only` to choose the scan worker setting and
`recommendations.processing` to choose derivative-processing workers when that
section is present. Record local CPU utilization, memory high-water mark, disk
queue or storage notes, and any I/O contention next to the aggregate JSON so the
highest-throughput worker count can be weighed against operating headroom. For
privacy-sensitive sample batches, share only aggregate counts, elapsed seconds,
throughput, effective worker count, worker mode, recommendation fields, and
finding totals. Do not share source images, filenames, thumbnails, per-file
records, or generated output directories from private image collections. Run
private sample benchmarks locally only, and publish aggregate
performance/statistical summaries without images, paths, thumbnails, or
row-level processing manifests.

### Production acceptance gate

Use `archive-scan-qc acceptance-summary` as the final aggregate-only gate before
batch delivery. It can combine any approved aggregate evidence files:
`run_plan_summary.json`, `review_summary.json`,
`processing_audit_summary.json`, `benchmark_results.json`, and
`aggregate_baseline_summary.json`.

```bash
archive-scan-qc acceptance-summary \
  --run-plan-summary /approved-work/project/run_plan_summary.json \
  --review-summary /approved-work/project/review_summary.json \
  --processing-audit-summary /approved-work/project/processing_audit_summary.json \
  --benchmark-results /approved-work/project/benchmark_results.json \
  --aggregate-baseline-summary /approved-work/project/aggregate_baseline_summary.json \
  --min-scan-files-per-minute 100 \
  --min-processing-files-per-minute 60 \
  --out /approved-work/project/acceptance_summary.json
```

The default gate blocks when remaining P0 findings, remaining P1 findings,
failed batches, or processing failed files are greater than zero. When an
aggregate baseline summary is supplied, the gate also blocks on a failed privacy
self-check, cleanup not being enabled, or cleanup retaining generated private
artifacts instead of only `aggregate_baseline_summary.json`. Throughput
thresholds are optional and configured with `--min-scan-files-per-minute` and
`--min-processing-files-per-minute`. Missing optional evidence produces warnings;
at least one aggregate evidence input is required.

`acceptance_summary.json` includes schema version, generation time, pass/fail
status, blocking items, warnings, P0/P1 remaining counts, failed batch count,
processing failure count, throughput and worker summaries, human review status,
and recommended next steps. It must not include source filenames, source
locations, hashes, thumbnails, row-level findings, reviewer notes, OCR/text, or
image content.

### Delivery handoff manifest

Use `archive-scan-qc delivery-manifest` to build a local evidence index for a
delivery package review. The command writes deterministic JSON and CSV manifests
with each selected file's path, name, byte size, SHA-256, detected
`schema_version` when available, and sensitivity classification. It never copies
source images, derivative images, or evidence files, and it does not upload
anything.

```bash
archive-scan-qc delivery-manifest \
  --scan-report /approved-work/project/scan_qc_report.json \
  --processing-audit-summary /approved-work/project/processing_audit_summary.json \
  --acceptance-summary /approved-work/project/acceptance_summary.json \
  --review-summary /approved-work/project/review_summary.json \
  --benchmark-results /approved-work/project/benchmark_results.json \
  --processing-manifest /approved-work/project/processing_manifest.json \
  --out /approved-work/project/handoff
```

The outputs are `delivery_handoff_manifest.json` and
`delivery_handoff_manifest.csv`. Missing artifact paths are rejected. Known
aggregate summaries such as `review_summary.json`,
`processing_audit_summary.json`, `benchmark_results.json`, and
`acceptance_summary.json` are marked `aggregate_public_safe`. Known row-level or
local review artifacts such as `scan_qc_report.json`,
`processing_manifest.json`, retry manifests, scan report CSV/HTML files, review
templates, and unknown extra artifacts are marked
`sensitive_local_evidence`.

### Release validation

```bash
PYTHONPATH=src python -m unittest discover -s tests
PYTHONPATH=src python -m compileall -q src tests
python3 scripts/validate_release.py
```

The local release validation runs unit tests, compileall, wheel creation, an
examples-based synthetic preflight followed by an end-to-end dry-run with
derivative processing, an isolated install smoke test, `archive-scan-qc
--version`, a synthetic benchmark, a synthetic acceptance smoke, and a
synthetic-image CLI scan. The dry-run uses only generated temporary images and
the committed privacy-safe files in `examples/`.

See `docs/operations-runbook.md` for production installation, directory,
privacy, troubleshooting, exit-code, and tuning guidance. See
`docs/release-checklist.md` before publishing a release.

### Privacy-safe examples

The `examples/` directory contains non-private release and deployment samples:

- `rules-profile.production-sample.json`: generic production-style rules
  profile.
- `manifest.sample.csv`: manifest with synthetic page names only.
- `batch-run.sample.sh`: command template that uses placeholder paths.

Do not replace these files with real collection names or private paths.
