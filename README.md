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
- duplicate filename and duplicate file-content checks
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
overwrites. The deskew implementation is also Pillow-only; it does not add
OpenCV, scikit-image, or other heavy native dependencies.

### Install

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

### Run

```bash
archive-scan-qc \
  --input /path/to/scanned-images \
  --out /path/to/qc-report \
  --process-out /path/to/processed-images \
  --auto-crop \
  --deskew \
  --project demo-project \
  --batch batch-001 \
  --min-dpi 200 \
  --name-pattern 'A001_\d{4}' \
  --manifest-csv /path/to/manifest.csv
```

The optional manifest CSV must include a `relative_path` column whose values
are expected image paths relative to `--input`.

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

When `--deskew` and `--auto-crop` are enabled together, the derivative pipeline
runs EXIF transpose, color conversion, skew detection, conservative deskew,
conservative crop, and then light autocontrast. Deskew runs before crop so the
manifest has a clear pre/post rotation size and crop can operate on the final
page orientation. The processing manifest records each image's
`skew_angle_degrees`, `skew_confidence`, `deskewed`, `deskew_reason`,
`pre_deskew_size`, `post_deskew_size`, crop bbox, crop decision, original size,
output size, operations, and failure reason. Auto crop and deskew are opt-in to
avoid surprising source batches; original images are still only read.

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
P0/P1/P2 finding counts, and manifest usage/missing/unexpected/duplicate
counts when a manifest is provided. JSON summary and manifest metadata include
skipped file and directory counts for auditability. The HTML report is a single
static file with inline CSS for manual review; it shows the batch manifest,
summary counts, skipped counts, file metadata table, and finding table with
P0/P1/P2 severity badges.

The process returns exit code `1` when P0 findings are present, so it can be
used in batch scripts. Reports are written to the output directory; original
images are only read.

### Test

```bash
PYTHONPATH=src python -m unittest discover -s tests
```
