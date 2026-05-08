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
are expected image paths relative to `--input`.

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

The optional `--process-out` directory enables the first local image-processing
layer. Source images remain read-only. The CLI writes derivative images under
`--process-out/images`, preserving source relative paths, and writes
`--process-out/processing_manifest.json` to link each derivative back to the
source SHA-256. The initial processing pipeline applies EXIF orientation
normalization, safe RGB/L color conversion, and light automatic contrast
normalization. Add `--auto-crop` with `--process-out` to enable conservative
Pillow-only page-border cropping for derivative images.

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
- `batch_orientation_consistency` P2 when a batch has a supported mix of
  portrait and landscape openable images after excluding square or near-square
  pages. The default is conservative and requires at least two files in each
  orientation class, so a single legitimate landscape attachment is not treated
  as a critical defect.

These thresholds are defaults in the built-in rules profile so batch-specific
callers can tune them through JSON without changing report structure. JSON
summary and manifest metadata include skipped file and directory counts for
auditability. The CSV file export includes the quality metric columns. The HTML
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
scan and processing elapsed seconds, files per minute, requested and effective
workers, operation flags, Python version, platform, CPU count, and best-effort
memory/GPU fields. They do not include source images, filenames, relative
paths, content hashes, thumbnails, per-file quality metrics, per-file finding
rows, single-file manifests, or image content.

For private local samples, share only `benchmark_results.json` and/or
`benchmark_results.csv`. Do not share the image directory, generated derivative
images, normal scan QC reports, processing manifests, filenames, thumbnails, or
row-level records from private collections. This makes it practical to compare
throughput on domestic platforms and different hardware while keeping sample
identity and content local.

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
count. For privacy-sensitive sample batches, share only aggregate counts,
elapsed seconds, throughput, effective worker count, worker mode, and finding
totals. Do not share source images, filenames, thumbnails, per-file records, or
generated output directories from private image collections. Run private sample
benchmarks locally only, and publish aggregate performance/statistical summaries
without images, paths, thumbnails, or row-level processing manifests.

### Release validation

```bash
PYTHONPATH=src python -m unittest discover -s tests
PYTHONPATH=src python -m compileall -q src tests
python3 scripts/validate_release.py
```

The local release validation runs unit tests, compileall, wheel creation, an
examples-based synthetic end-to-end dry-run with derivative processing, an
isolated install smoke test, `archive-scan-qc --version`, and a synthetic-image
CLI scan. The dry-run uses only generated temporary images and the committed
privacy-safe files in `examples/`.

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
