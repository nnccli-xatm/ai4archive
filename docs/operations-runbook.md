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

For project-scale production runs, create a local run plan instead of launching
each batch manually. CSV example:

```csv
batch_id,input_dir,report_dir,process_out,manifest_csv,rules_profile,workers,auto_crop,deskew,trim_dark_border,despeckle,resume_processing
batch-001,/approved-work/input-batches/batch-001,batch-001,/approved-work/processed-derivatives/batch-001,/approved-work/manifests/batch-001.csv,/approved-work/rules/project-rules.json,2,true,true,true,true,false
batch-002,/approved-work/input-batches/batch-002,batch-002,/approved-work/processed-derivatives/batch-002,/approved-work/manifests/batch-002.csv,/approved-work/rules/project-rules.json,2,true,true,true,true,true
```

Run it with:

```bash
archive-scan-qc run-plan \
  --plan-csv /approved-work/plans/project-run.csv \
  --out /approved-work/qc-reports/project-code \
  --project project-code \
  --continue-on-error
```

JSON plans may be a top-level list of batch objects or an object with
`project_id` and `batches`. Required batch fields are `batch_id` and
`input_dir`. Optional fields are `report_dir`, `process_out`, `manifest_csv`,
`rules_profile`, `workers`, `min_dpi`, `name_pattern`, `auto_crop`, `deskew`,
`trim_dark_border`, `despeckle`, and `resume_processing`. Relative input,
manifest, and rules-profile paths resolve relative to the plan file; relative
report and processing-output paths resolve under the project `--out` root.

`run-plan` performs preflight first for every batch it attempts. A preflight
failure prevents that batch's scan and processing steps. Without
`--continue-on-error`, the command stops after the first failed batch. With
`--continue-on-error`, later batches still run, and the final exit code remains
non-zero if any batch failed.

## Reports And Archival Evidence

Each scan writes:

- `preflight_report.json` when `archive-scan-qc preflight` is run
- `scan_qc_report.json`
- `scan_qc_report.html`
- `scan_qc_files.csv`
- `scan_qc_findings.csv`

When derivative processing is enabled, `--process-out` also contains:

- `processing_manifest.json`
- `processing_retry_manifest.json`
- `processing_audit_summary.json`
- `images/` with derivative images preserving source relative paths

Each project run plan additionally writes under its project `--out` root:

- `run_plan_summary.json`
- `run_plan_summary.csv`

Archive the command line, rules profile, manifest CSV, JSON report, HTML
report, CSV exports, processing manifest, retry manifest, audit summary,
package version, Python version, Pillow version, platform, and worker setting.
Treat row-level reports, processing manifests, and retry manifests as sensitive
because they include filenames, relative paths, hashes, and per-file metrics.
`processing_audit_summary.json` is aggregate-only: it records counts, operation
flags, worker metadata, timing, throughput, failure totals, and resume counts
without file lists, paths, hashes, thumbnails, or image content.
`run_plan_summary.json` and `run_plan_summary.csv` are also aggregate-only:
they record batch totals, passed and failed counts, P0/P1/P2 counts,
processing failure counts, preflight error counts, throughput aggregates, and
failed batch IDs. They intentionally omit source filenames, source paths,
hashes, thumbnails, row-level file metadata, and image content. Keep the run
plan file itself local if its paths reveal private collection locations.

## Interrupted Processing Recovery

Use normal reruns when a deliberate full overwrite/reprocess is required. The
default scan/process command keeps that behavior.

Use `--resume-processing` when a derivative batch was interrupted and the
existing `--process-out` directory should be reused:

```bash
archive-scan-qc \
  --input /approved-work/input-batches/batch-001 \
  --out /approved-work/qc-reports/batch-001 \
  --process-out /approved-work/processed-derivatives/batch-001 \
  --resume-processing \
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

Resume mode reads the existing `processing_manifest.json`. It skips files that
were previously successful and whose derivative still exists, and it reprocesses
previous failures, skipped records, and missing derivative outputs. After the
run, review stdout plus `processing_audit_summary.json` for
`skipped_due_to_resume`, `reprocessed_files`, `failed_files`, and
`retry_list_files`. Use `processing_retry_manifest.json` only inside the
approved private environment to identify remaining failed files for another
fix-and-resume cycle.

Use the JSON `rule_catalog` object or the HTML Rule Catalog section when
explaining why a finding exists. The catalog gives the rule title, default
severity, DA/T 31-2017 clause-numbered short reference such as
`DA/T 31-2017 10.5.1`, check target, automation status, and a plain-language
explanation. The catalog is static metadata and is safe to share publicly; the
row-level reports that contain filenames, paths, hashes, and per-file metrics
are not.

For standards traceability reviews, use `docs/standards-traceability.md` as the
crosswalk between DA/T 31-2017 clauses and implementation evidence. Confirm the
active rules profile and report `rule_catalog` use the same clause references as
that document before accepting a release candidate or changing production
thresholds.

## Offline Analysis Provider

The scan command can optionally run a local offline analysis provider with
`--analysis-provider-command`. This is disabled by default; omitting the flag is
the approved way to disable provider analysis and preserve the built-in
rules-only path.

Only use providers installed in the approved local environment. The scanner
sends JSONL to the provider process on stdin with local file paths and basic
run/image metadata. It does not send image bytes, thumbnails, OCR text, hashes,
or file content. Providers must not use the network or upload images, derived
text, thumbnails, or metadata outside the controlled environment.

Provider output must be JSONL on stdout. Findings must include `relative_path`,
`rule`, `severity`, `confidence`, `message`, and optional safe `metadata`.
Provider rules must use `provider.<name>.<rule>`; built-in rules and protected
P0 checks cannot be overridden by a provider. Invalid output stops the run with
a clear CLI error before reports are written.

Provider findings are merged with built-in findings and marked with
`source=provider`; built-in rule findings are marked with `source=rules`.
Reports show provider aggregate metadata and provider finding counts, but must
not include embedded images, thumbnails, OCR text, file content, or provider
metadata fields that carry row-level content. Validate this boundary with
synthetic fake providers before enabling a real local model.

## Human Review And Acceptance Summary

When automated QC produces findings, create a local review template for manual
disposition:

```bash
archive-scan-qc review-export \
  --report /approved-work/qc-reports/batch-001/scan_qc_report.json \
  --out /approved-work/qc-reports/batch-001/review_template.csv
```

The template may also be written as JSON by using a `.json` output path. Its
stable fields are `finding_id`, `rule`, `severity`, `relative_path`, `status`,
and `reviewer_notes`. Reviewers update `status` to `pending`, `accepted`,
`false_positive`, `fixed`, or `needs_rescan`. Use `reviewer_notes` only inside
the approved local environment for disposition evidence.

After review is complete, create the aggregate-only acceptance artifact:

```bash
archive-scan-qc review-summary \
  --review /approved-work/qc-reports/batch-001/review_template.csv \
  --out /approved-work/qc-reports/batch-001/review_summary.json
```

`review_summary.json` includes severity, rule, status, severity/status, and
rule/status counts, remaining P0/P1 counts, and `acceptance_passed`. It contains
no row-level path list, filenames, hashes, messages, or reviewer notes. P0/P1
findings remain open unless their status is `fixed` or `false_positive`.
Acceptance passes only when remaining P0 and remaining P1 counts are both zero.

## Aggregate Rule Calibration

Use rule calibration only as a local aggregate analysis loop. The workflow is:
run automated QC, complete human review, generate `review_summary.json`, run
aggregate calibration, then submit any rules profile change for human approval.
Do not use real private samples in public evidence, and do not upload source
reports or review templates.

```bash
archive-scan-qc calibrate-rules \
  --report /approved-work/qc-reports/batch-001/scan_qc_report.json \
  --review-summary /approved-work/qc-reports/batch-001/review_summary.json \
  --out /approved-work/qc-reports/batch-001/rules_calibration_summary.json
```

Multiple `--report` and `--review-summary` arguments may be supplied when
calibrating across local batches. If the aggregate review summary has not been
created yet, a filled local review template may be supplied with `--review`;
that template remains sensitive input and only aggregate counts are emitted.

The output includes per-rule trigger counts, severity distribution, optional
manual disposition status distribution, conservative recommendations such as
`keep`, `tighten`, `loosen`, or `need_more_samples`, and a field summary of the
active rules profile. It must not include source file paths, filenames, hashes,
OCR text, image content, thumbnails, row-level messages, or reviewer notes.
`scan_qc_report.json` remains sensitive; `rules_calibration_summary.json` is the
aggregate evidence artifact after local policy review.

To create a draft profile suggestion without changing the original profile:

```bash
archive-scan-qc calibrate-rules \
  --report /approved-work/qc-reports/batch-001/scan_qc_report.json \
  --review-summary /approved-work/qc-reports/batch-001/review_summary.json \
  --out /approved-work/qc-reports/batch-001/rules_calibration_summary.json \
  --write-suggested-profile /approved-work/qc-reports/batch-001/rules-profile.suggested.json
```

The suggested profile is marked `draft` and `suggested`; it is not a production
profile. A project lead must compare it with the original profile and approve
any threshold, severity, or enablement changes before deployment.

Sensitive local evidence includes source images, derivative images,
`scan_qc_report.json`, `scan_qc_report.html`, `scan_qc_files.csv`,
`scan_qc_findings.csv`, `processing_manifest.json`, and review templates. Keep
these inside the approved environment. `review_summary.json` is designed as the
shareable aggregate acceptance evidence, but still review it for local policy
before external release.

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
- Review `processing_retry_manifest.json` for the local failed-file retry list.
- Rerun with `--resume-processing` after fixing source or environment issues so
  existing successful derivatives are skipped.
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
