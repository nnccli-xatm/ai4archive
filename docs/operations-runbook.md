# Archive Scan QC Operations Runbook

This runbook covers production use of the `archive-scan-qc` CLI for local
archive scan quality checks and optional derivative processing. It is scoped to
the current implementation: Pillow-based local checks, JSON/HTML/CSV reports,
manifest validation, and conservative local processing. It does not require
private samples, network services, hosted models, or public cloud access.

## Installation

Use Python 3.10, 3.11, or 3.12. Create an isolated environment and install the
package from a trusted source or local wheelhouse.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install .
archive-scan-qc --version
```

For offline deployment, build wheels on a connected build machine that matches
the target platform family, copy the wheelhouse through the approved media
process, and install without using the public package index.

```bash
python -m pip wheel --wheel-dir wheelhouse .
python -m pip download --only-binary=:all: --dest wheelhouse 'Pillow>=10,<13'
python -m pip install --no-index --find-links wheelhouse ai4archive
```

If Pillow has no prebuilt wheel for the target CPU or OS, build and validate an
internal Pillow wheel with the required native image libraries before rollout.

## Cross-Platform Operation

The CLI runs on Windows, macOS, Linux, and domestic Linux distributions when a
supported Python and Pillow build are available. Use the same command structure
across platforms, but adjust path syntax and virtual environment activation.

Windows:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install .
archive-scan-qc --version
```

POSIX shells:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
archive-scan-qc --version
```

Use UTF-8 capable terminals and avoid shell glob expansion in production
commands. Pass directories to `--input`, `--out`, and `--process-out`; do not
expand image files on the command line.

## Domestic Platform Notes

For UOS, Kylin, openKylin, aarch64, and LoongArch environments:

- Prefer an internal wheelhouse with pinned, tested wheels for Python, Pillow,
  and `ai4archive`.
- Validate the exact OS image, CPU architecture, Python minor version, and
  Pillow wheel before accepting a production node.
- Keep `--workers` conservative until local memory and I/O throughput are
  measured.
- Record platform, CPU count, memory, Python version, Pillow version, worker
  count, elapsed seconds, and files per minute in the release evidence.
- Do not rely on GPU availability for the current CLI; current scan and
  derivative processing are CPU-based.

## Resource Guidance

Minimum for small batches:

- CPU: 4 cores
- Memory: 8 GB
- Storage: local SSD preferred
- GPU: not required

Recommended production workstation:

- CPU: 8 to 16 cores
- Memory: 32 to 64 GB
- Storage: NVMe SSD for input, reports, and derivative output
- GPU: not required by the current implementation

Large images and high worker counts increase memory and disk pressure. Start
with `--workers 1`, then test `--workers 2`, `--workers 4`, and the local
upper bound while watching memory, CPU saturation, disk queue length, and
throughput. For production acceptance, run `archive-scan-qc benchmark` across
the candidate worker counts and use `benchmark_results.json` `recommendations`
as the aggregate capacity-planning summary. Treat the recommended workers as
the throughput optimum from that run, then confirm there is enough CPU, memory,
and storage headroom before standardizing it.

## Directory Conventions

Keep source scans, reports, and derivatives in separate directories.

```text
/approved-work/
  input-batches/
    batch-001/
      BATCH001_PAGE_0001.png
      BATCH001_PAGE_0002.png
  manifests/
    batch-001.csv
  qc-reports/
    batch-001/
  processed-derivatives/
    batch-001/
```

Recommended command:

```bash
archive-scan-qc preflight \
  --input /approved-work/input-batches/batch-001 \
  --out /approved-work/qc-reports/batch-001 \
  --process-out /approved-work/processed-derivatives/batch-001 \
  --auto-crop \
  --deskew \
  --trim-dark-border \
  --despeckle \
  --workers 2 \
  --project project-code \
  --batch batch-001 \
  --manifest-csv /approved-work/manifests/batch-001.csv \
  --rules-profile /approved-work/rules/project-rules.json

archive-scan-qc \
  --input /approved-work/input-batches/batch-001 \
  --out /approved-work/qc-reports/batch-001 \
  --process-out /approved-work/processed-derivatives/batch-001 \
  --auto-crop \
  --deskew \
  --trim-dark-border \
  --despeckle \
  --workers 2 \
  --project project-code \
  --batch batch-001 \
  --manifest-csv /approved-work/manifests/batch-001.csv \
  --rules-profile /approved-work/rules/project-rules.json
```

The manifest CSV must contain a `relative_path` column with paths relative to
`--input`. Keep `--out` and `--process-out` outside the input tree where
possible. If output is inside the input tree, the scanner skips the known output
directory on reruns, but separate directories are easier to audit.

Run preflight before the full scan/process command for each production batch.
Preflight does not open images, copy files, modify originals, or create
derivatives. It writes `preflight_report.json` under `--out` with aggregate
configuration, candidate count, skipped count, manifest count, error, and warning
fields only. Do not treat it as a scan report; it is a go/no-go configuration
and manifest risk check.

## Reports And Archival Evidence

Each scan writes:

- `preflight_report.json` when `archive-scan-qc preflight` is run
- `scan_qc_report.json`
- `scan_qc_report.html`
- `scan_qc_files.csv`
- `scan_qc_findings.csv`

When derivative processing is enabled, `--process-out` also contains:

- `processing_manifest.json`
- `images/` with derivative images preserving source relative paths

Archive the command line, rules profile, manifest CSV, JSON report, HTML
report, CSV exports, processing manifest, package version, Python version,
Pillow version, platform, and worker setting. Treat row-level reports and
processing manifests as sensitive because they include filenames, relative
paths, hashes, and per-file metrics.

Use the JSON `rule_catalog` object or the HTML Rule Catalog section when
explaining why a finding exists. The catalog gives the rule title, default
severity, DA/T 31-2017 short reference, check target, automation status, and a
plain-language explanation. The catalog is static metadata and is safe to share
publicly; the row-level reports that contain filenames, paths, hashes, and
per-file metrics are not.

For standards traceability reviews, use `docs/standards-traceability.md` as the
crosswalk between DA/T 31-2017 areas and implementation evidence. Confirm the
active rules profile and report `rule_catalog` match that document before
accepting a release candidate or changing production thresholds.

## Exit Codes

For `archive-scan-qc preflight`:

- `0`: preflight checks passed.
- `1`: preflight found a fatal production risk, such as missing input, invalid
  output path, processing flags without `--process-out`, invalid workers,
  invalid rules profile, unsafe manifest paths, duplicate manifest entries, or
  manifest missing/unexpected file counts.
- `2`: CLI argument parsing error from `argparse`.

Warnings in `preflight_report.json` are runnable but risky conditions, such as
placing outputs under the input tree. Errors are conditions that should be fixed
before the production scan/process command.

For the scan/process command:

- `0`: scan completed and no P0 findings were present.
- `1`: scan completed and at least one P0 finding was present, such as
  unopenable images, manifest mismatches, duplicate file content, or DPI below
  the active minimum.
- `2`: CLI argument or configuration error from `argparse`, such as invalid
  rules profile JSON, missing required arguments, or processing flags without
  `--process-out`.
- Other non-zero codes: unexpected runtime failure from Python, the OS, or the
  shell wrapper. Preserve stdout, stderr, and the working directory for
  diagnosis.

## Privacy Requirements

Run private archive batches only on approved local or internal machines. Do not
upload or paste source images, derivative images, filenames, directory lists,
hashes, thumbnails, row-level JSON/CSV/HTML reports, or processing manifests to
public systems.

Use `examples/` and temporary synthetic images for public PRs, issues, release
notes, and dry-run evidence. For private performance comparison, share only
aggregate benchmark fields such as total file count, openable count, finding
counts, elapsed seconds, files per minute, effective workers, CPU count,
platform, Python version, Pillow version, and benchmark recommendation fields.
Private samples are acceptable for internal capacity planning only because the
shared artifact is aggregate-only; never include filenames, paths, directory
listings, hashes, thumbnails, row-level reports, or derivative images.

## Release Candidate Dry-Run

Run the local release validator before tagging or publishing a candidate:

```bash
python3 scripts/validate_release.py
```

The validator runs unit tests, compileall, wheel creation, an examples-based
synthetic preflight followed by an end-to-end dry-run with derivative
processing, an isolated package install, `archive-scan-qc --version`, and a
synthetic smoke scan. It uses only temporary generated images and the committed
non-private files in `examples/`.

If build isolation is unavailable in the target environment, use:

```bash
python3 scripts/validate_release.py --skip-build-artifacts
```

Use the skip option only for local diagnosis. Release evidence should include a
full run with wheel creation.

## Troubleshooting

Installation fails on Pillow:

- Confirm Python version and CPU architecture.
- Use an approved wheelhouse that contains a compatible Pillow wheel.
- If no wheel exists, build Pillow internally with the required native image
  libraries and record the build provenance.

CLI exits with code `1`:

- Open `scan_qc_report.html` locally.
- Review P0 findings first.
- Check manifest missing, unexpected, and duplicate rows.
- Confirm DPI metadata and the active `min_dpi` in `rules_profile` metadata.

Rules profile is rejected:

- Validate that the file is JSON object syntax.
- Confirm `min_dpi` is an integer, thresholds are numbers, rule `enabled`
  values are booleans, and severities are `P0`, `P1`, or `P2`.
- Run with the committed sample profile to separate profile syntax from
  environment issues.

Throughput is lower than expected:

- Move input and output to local SSD or NVMe storage.
- Avoid writing reports and derivatives over slow network shares.
- Increase `--workers` gradually and stop when files per minute stops
  improving or memory pressure rises.
- Benchmark scan-only and processing modes separately.
- Review `benchmark_results.json` `recommendations.scan_only` and
  `recommendations.processing`; a diminishing-return note means a higher worker
  count had less than the benchmark threshold of adjacent throughput gain and
  should be accepted only with local CPU, memory, and I/O evidence.

Derivative processing fails for some files:

- Confirm the source image remained unchanged and readable after scanning.
- Review `processing_manifest.json` for `status`, `failure_reason`, and
  per-file operation decisions.
- Rerun with `--workers 1` to simplify local diagnosis.

Unexpected report content after rerun:

- Use a fresh `--out` directory per run when producing release evidence.
- Keep reports outside `--input`.
- Confirm hidden directories and the manifest CSV location are not being
  mistaken for image inputs.

## Performance Tuning Parameters

Start with these controls:

- `--workers`: primary control for CPU and memory pressure.
- `--process-out`: enables derivative writing; omit for scan-only checks.
- `--auto-crop`: conservative page-border crop.
- `--deskew`: conservative small-angle deskew.
- `--trim-dark-border`: dark edge trim before crop.
- `--despeckle`: isolated dark speckle cleanup.
- `--rules-profile`: project-specific DPI, filename, and quality thresholds.
- `archive-scan-qc benchmark`: aggregate-only local throughput comparison.

Record both requested and effective workers from stdout or report performance
metadata. The CLI caps effective workers by CPU count, batch size, and an
internal safety ceiling.

For capacity planning, prefer the aggregate benchmark recommendations over a
single manual run. Record the recommended requested workers, effective workers,
files/min, diminishing-return notes, CPU utilization, memory high-water mark,
storage type, disk queue or I/O wait observations, and whether scanning and
processing were benchmarked separately. Share only the aggregate JSON/CSV and
those operational notes outside the private environment.
