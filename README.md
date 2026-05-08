# ai4archive

AI tools for archive workflows.

## Scan QC phase-one CLI

This repository now includes a minimal local CLI for the first implementation
phase described in `archive-scan-qc-retouch-design.md`: standard rules and a
batch-processing skeleton for scanned image quality control.

The current implementation covers:

- project and batch identifiers for an imported image directory
- optional CSV catalog import and image-to-catalog matching
- recursive image-directory import without modifying source files
- file openability checks
- image format, DPI, color mode, width, height, file size, and SHA-256 capture
- duplicate filename and duplicate file-content checks
- batch-level format, DPI, and color-mode consistency findings
- JSON report plus CSV file and finding exports

### Dependency choice

The CLI uses Pillow for local image openability and metadata extraction. Pillow
is open source, lightweight, cross-platform, and works offline. Hashing, JSON,
CSV, path handling, and rule checks use only the Python standard library. This
keeps phase one aligned with the design constraints: no cloud services, no
large frontend framework, no generative image repair, and no original-image
overwrites.

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
  --project demo-project \
  --batch batch-001 \
  --min-dpi 200 \
  --name-pattern 'A001_\d{4}' \
  --catalog /path/to/catalog.csv
```

The optional catalog CSV must include one image path column named
`relative_path`, `path`, `filename`, `file`, or `image`. Paths are matched
relative to the scanned-image input directory.

The command writes:

- `scan_qc_report.json`
- `scan_qc_files.csv`
- `scan_qc_findings.csv`

The process returns exit code `1` when P0 findings are present, so it can be
used in batch scripts. Reports are written to the output directory; original
images are only read.

### Test

```bash
PYTHONPATH=src python -m unittest discover -s tests
```
