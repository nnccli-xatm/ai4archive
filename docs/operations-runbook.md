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
- Treat model/deep-inspection providers as disabled unless an approved local
  provider implementation has passed the aggregate-only provider probe and a
  separate privacy review.

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
  --workers 2 \
  --project project-code \
  --batch batch-001 \
  --manifest-csv /approved-work/manifests/batch-001.csv \
  --rules-profile /approved-work/rules/project-rules.json

archive-scan-qc processing-plan \
  --report /approved-work/qc-reports/batch-001/scan_qc_report.json \
  --input /approved-work/input-batches/batch-001 \
  --out /approved-work/qc-reports/batch-001/processing-plan \
  --auto-crop \
  --deskew \
  --trim-dark-border \
  --despeckle

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

For the stable external CLI batch-service path, prefer `production-run` for a
single batch. It writes `production_run_progress.json` while running and always
uses a dedicated metadata directory for production status, summaries, admin
reports, and recovery evidence:

```bash
archive-scan-qc production-run \
  --input /approved-work/input-batches/batch-001 \
  --derivatives-out /approved-work/processed-derivatives/batch-001 \
  --metadata-out /approved-work/processed-derivatives/batch-001/_production_workbench \
  --project project-code \
  --batch batch-001 \
  --manifest-csv /approved-work/manifests/batch-001.csv \
  --rule-template dat-31-2017-standard \
  --workers 2
```

Built-in rule templates include `archival-safe-v1`,
`text-clean-readable-v1`, `print-clean-v1`, and `photo-mixed-safe-v1`. Legacy
IDs `dat-31-2017-standard`, `text-clean-print`, and `high-fidelity-original`
remain supported for existing run plans. Use `--rule-template custom
--rules-profile /approved-work/rules/project-rules.json` for a project-specific
JSON template. `text-clean-readable-v1` and legacy `text-clean-print` enable the
current text cleanup operations and disable the despeckle photo/mixed-content
preservation gate. Do not use them for photos, drawings, stamps-heavy pages, or
other high-fidelity original material; use `photo-mixed-safe-v1`,
`high-fidelity-original`, or a custom profile instead.

Before exposing a template to operators or an external scheduler, generate the
public-safe catalog and dry-run plan:

```bash
archive-scan-qc rule-template-catalog \
  --out /approved-work/validation/rule-template-catalog

archive-scan-qc rule-template-dry-run \
  --rule-template text-clean-readable-v1 \
  --scan-report /approved-work/qc-report/scan_qc_report.json \
  --out /approved-work/validation/rule-template-dry-run
```

The dry-run may read the local sensitive scan report, but it does not run image
processing or write derivative images. Its JSON output contains only aggregate
file/finding counts, planned operation stages, risk codes, and privacy flags; it
must not include paths, filenames, hashes, OCR text, thumbnails, image content,
or row-level evidence.

Template metadata is recorded in the production summary and scan report. If a
run fails before completion, the progress file moves to `failed` or
`interrupted` with a failure object and a recovery-oriented production summary;
external schedulers should read the JSON state instead of inferring status only
from the process table.

For the service-oriented job boundary MVP, keep each externally submitted job
inside its own `service_root/jobs/<job_id>/` directory. The service core creates
separate `metadata`, `derivatives`, `tmp`, `checkpoints`, `review`, and `logs`
subdirectories and rejects service roots that overlap the input directory.
The in-process `archive_scan_qc.service_api` module defines the public-safe
response shapes. The prototype local HTTP transport can be started with
`archive-scan-qc service-api --service-root <approved-service-root> --host
127.0.0.1 --port 8765` and serves `GET /api/health`, `GET /api/capabilities`,
`GET /api/rule-templates`, `GET /api/rule-templates/{template_id}`,
`POST /api/rule-templates/validate`, `POST /api/rule-templates`,
`PUT /api/rule-templates/{template_id}`, `POST /api/jobs`, `GET /api/jobs`,
`GET /api/jobs/{job_id}`,
`GET /api/jobs/{job_id}/local-review/{artifact_id}`,
`POST /api/jobs/{job_id}/run`, `POST /api/jobs/{job_id}/start`,
`POST /api/jobs/{job_id}/retry`, `POST /api/jobs/{job_id}/cancel`,
`GET /api/jobs/{job_id}/review-history`,
`GET /api/production/session`, `POST /api/production/setup`,
`POST /api/production/start`, `GET /api/production/progress`,
`GET /api/production/review-queue`, `GET /api/production/review-history`, and
`POST /api/production/finish-export`.
The rule-template endpoints expose the same public-safe catalog and no-image
dry-run plan as the CLI rule-template commands. The validate endpoint accepts
only an inline custom template draft and returns aggregate validation counts and
risk codes; it does not write templates, accept local profile paths, or echo
name patterns/rule rows. The custom template write endpoints save
service-managed custom templates under server-owned storage and return only
template IDs, validation counts, risk codes, and processing-default booleans.
The HTTP transport is local-only and uses the configured service root; reject
requests that try to provide their own `service_root`.
The production endpoints are a public-safe facade over the job boundary:
`setup` creates a job, `start` enters the async runner, `progress` polls by
`job_id`, `review-queue` returns aggregate local-review availability and group
counts, `review-actions` writes the local decision summary, aggregate
verification summary, and append-only `service_job_review_history.json` under
the isolated job `review` directory, and `finish-export` returns a
completion/export readiness summary. Responses expose only aggregate
review-history counts and latest verification status; they do not return
row-level local review records, local IDs, paths, or raw decision rows.
Use the dedicated review-history endpoints when a frontend only needs to refresh
aggregate review history after an action; do not read the local history JSON
directly outside the service boundary.
Use `GET /api/jobs/{job_id}/local-preview/{local_id}?source=original|processed`
or `GET /api/production/preview?job_id=...&local_id=...&source=...` only inside
the local workstation session to show preview image bytes. The service resolves
the local ID through the job's production review queue and validates the final
file under the authorized input or derivatives directory. Do not copy preview
URLs, image bytes, local IDs, filenames, or headers into public evidence.
The `run` endpoint is synchronous: clients should expect the request to return
after the existing production runner reaches a terminal state, then poll the job
status or index for public-safe counts and quality summary fields. The `start`
endpoint is the first local in-process async MVP: it returns a `running` public
summary immediately and executes the same production runner in a background
thread. While the service process is alive, polling keeps active jobs in
`running`; after a service restart, stale `running` checkpoints still recover as
`needs_recovery`. The service capabilities response publishes the current
`max_active_async_jobs`, `max_active_workers`, `min_free_space_bytes`,
`max_tmp_bytes_per_job`, and per-job worker limits. Job creation checks the
service-root free-space threshold. Async `start` checks active-job,
active-worker, and per-job temp limits before marking a job `running`, so
rejected jobs remain in their prior public state.
Use `POST /api/jobs/{job_id}/retry` only for jobs already in `failed`,
`interrupted`, or `needs_recovery`; it is synchronous in the prototype and
keeps the same job root so production resume semantics can reuse completed
derivatives and manifests.
Use `POST /api/rule-templates` and `PUT /api/rule-templates/{template_id}` to
save service-managed custom templates. The service owns the storage path; public
responses return only the template ID, validation counts, risk codes, and
processing-default booleans. Saved custom templates appear in catalog/detail
responses and can be selected by service jobs by template ID.
`service_job.json` is private checkpoint state because it contains local paths
and the template snapshot needed for recovery. Each job root includes isolated
`metadata`, `derivatives`, `tmp`, `checkpoints`, `review`, and `logs`
subdirectories. `service_job_public_summary.json` is the public-safe
polling/handoff shape: aggregate state, counts, quality category signals,
whitelisted quality metrics including before/after deltas and changed-pixel
ratios, guardrail summary, timing summaries,
source-integrity counts, isolation booleans, recovery status, and explicit
privacy flags only. Its public `counts`
block includes aggregate retry/reuse fields (`resumed_files`, `reused_files`,
`reprocessed_files`, and `retry_list_files`) so schedulers can confirm resume
behavior without opening private manifests. Its public `retry` block exposes
only retry availability, attempt number, status, and resume/reuse booleans; it
does not include paths, manifest rows, or retry file lists. Its nested
`timings` block uses schema
`scan-qc.service-job-public-timings.v1` and includes only whitelisted stage IDs,
aggregate processing throughput, and whitelisted operation timing fields. Its
nested `source_integrity` block uses schema
`scan-qc.service-job-source-integrity.v1` and includes only checked, unchanged,
modified, missing, and added source-image counts plus source-change booleans.
It never publishes source hashes or file lists. Keep the service API regression
covering nested Chinese/space paths, completed production processing, unchanged
source hashes, and public responses with those private path segments omitted.
If recovery sees a stale
`running` progress file after a service restart, it reports `needs_recovery`
instead of leaking paths or leaving the job silently running.
After production processing, service jobs write local-only
`processing_review_package.json`, `processing_review_package.html`, and
`production_review_queue.json` into the isolated `review` directory. These files
can contain row-level paths and operator context. The public summary exposes
only review availability, aggregate item counts, source-category counts, and
action counts. The local processing review package also groups background
cleanup, readability improvement, defect cleanup, and original appearance risk
separately; public service summaries may expose only those group counts.
Recovery also treats a private `service_job.json` checkpoint that still says
`running` but has no progress file as `needs_recovery`, and it rejects a tampered
checkpoint whose `input_dir` now overlaps the service root.
Recovery also requires successful terminal states (`finished` and
`needs_review`) to have a readable terminal `production_run_summary.json`. If a
checkpoint or progress file claims success without that summary, the public
state becomes `needs_recovery` with reason code
`terminal_state_missing_production_summary`.
For terminal jobs, recovery also regenerates missing local review artifacts from
the existing production summary and processing manifest when possible, then
refreshes the public review availability summary without exposing paths.
Use `GET /api/jobs/{job_id}/local-review/{artifact_id}` only from the local
service API to read sensitive review JSON artifacts. The accepted artifact IDs
are `processing-review-package` and `production-review-queue`; the service
validates the artifact path under the isolated job `review` directory and marks
the response `local_only`, `sensitive`, and `public_safe=false`. Do not expose
the service API on a non-loopback host.
Treat HTTP 404 `job_not_found` as a missing checkpoint/job-id condition. Treat
400 `input_dir_missing` only as a job creation input authorization or existence
problem.
Treat HTTP 404 `rule_template_not_found` as a missing service-managed custom
template. The service must reject that request before writing a partial job
checkpoint or job directory; invalid template IDs still use the public-safe
400 validation path.
Use service-job cancellation only to mark a non-terminal job as stopped; the
current synchronous runner cannot interrupt an in-flight image operation from a
separate request, but it records `cancelled` as a terminal public-safe state and
prevents accidental reruns.
Service job creation rejects worker counts below 1 or above the configured
per-job limit; the public summary includes only the requested worker count,
per-job limit, global active-worker limit, minimum free-space threshold, and
per-job temp limit, not local paths or process details.
Each service job writes a local-only `service_job_event_log.json` under the
isolated `logs` directory. Use the public `events` summary for monitoring:
event count, latest event type, latest state, and latest recovery status only.
Do not publish event rows or the local log path.
When recovering the full service root, read
`service_job_index_public_summary.json` for aggregate job state counts and
index-level quality/source-integrity status counts plus per-job public
summaries; it is designed for polling without exposing local paths.
The production facade `GET /api/production/session` exposes the same root-level
`quality` and `source_integrity` aggregate blocks under `session`, so UI or
worker clients can poll batch health without fetching each job summary.
If individual checkpoints are invalid or unreadable, the same index reports
only `skipped_job_count` and aggregate `recovery_issues.by_code`. Treat those
codes as restart triage signals; do not publish skipped job IDs, checkpoint
rows, local paths, or exception messages.

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

Run `archive-scan-qc processing-plan` after the scan report and before enabling
`--process-out`. It opens images for in-memory analysis only and writes
`processing_plan.json` plus `processing_plan.csv` under its `--out` directory.
The plan records per-file proposed EXIF transpose, deskew, dark-border trim,
auto-crop, despeckle, optional tone normalization, optional conservative
edge-shadow lightening, optional conservative background stain lightening,
optional conservative scanline lightening,
skipped, and unopenable decisions so operators can review the intended
processing before derivative files are created. Treat the plan as sensitive
local evidence because it contains row-level paths and hashes. It does not embed
images, thumbnails, or image bytes.

`--lighten-edge-shadow` is default-off and only targets narrow neutral shadows
at the page edge. It no-ops when dark marks, text-like pixels, annotations,
binding holes, color content, deep/dark pages, broad uneven lighting, or low
confidence would risk changing正文,边注,印章,批注, or archival appearance.

`--lighten-background-stains` is default-off and only targets small neutral,
low-contrast light stains on otherwise light paper background. It no-ops for
near-text candidates, red stamps, colored marks, annotations, binding or edge
marks, large or history-like stains, normal pages, dark pages, obvious color
pages, broad uneven lighting, sparse/low-confidence foreground evidence, or any
case that could alter正文、印章、批注、装订痕迹 or archival appearance.

`--lighten-scanlines` is default-off and only targets continuous, neutral,
low-contrast horizontal or vertical scanlines on light background areas. It
no-ops when a candidate touches or approaches正文、表格线、页码、边注、印章、彩色标记、
批注、装订孔、边缘暗痕、历史纸张痕迹, dark pages, obvious color pages, broad uneven
lighting, sparse/low-confidence foreground evidence, or any case where the line
could be archival content rather than scanner noise.

For project-scale production runs, create a local run plan instead of launching
each batch manually. CSV example:

```csv
batch_id,input_dir,report_dir,process_out,manifest_csv,rule_template,rules_profile,workers,auto_crop,deskew,trim_dark_border,despeckle,lighten_edge_shadow,lighten_background_stains,lighten_scanlines,resume_processing
batch-001,/approved-work/input-batches/batch-001,batch-001,/approved-work/processed-derivatives/batch-001,/approved-work/manifests/batch-001.csv,dat-31-2017-standard,,2,false,false,false,false,false,false,false,true
batch-002,/approved-work/input-batches/batch-002,batch-002,/approved-work/processed-derivatives/batch-002,/approved-work/manifests/batch-002.csv,custom,/approved-work/rules/project-rules.json,2,true,true,true,true,false,false,false,true
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
`rule_template`, `rules_profile`, `workers`, `min_dpi`, `name_pattern`,
`auto_crop`, `deskew`, `trim_dark_border`, `despeckle`, `normalize_tones`,
`lighten_edge_shadow`, `lighten_background_stains`, `lighten_scanlines`, and
`resume_processing`.
Relative input, manifest, and rules-profile paths resolve relative to the plan file; relative
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
- `processing_quality_summary.json`
- `images/` with derivative images preserving source relative paths

Each project run plan additionally writes under its project `--out` root:

- `run_plan_summary.json`
- `run_plan_summary.csv`

Archive the command line, rules profile, manifest CSV, JSON report, HTML
report, CSV exports, processing plan, processing manifest, retry manifest, audit summary,
package version, Python version, Pillow version, platform, and worker setting.
When a rule template was selected, `processing_manifest.json` records a
sanitized `rule_template` snapshot with template ID, version, source, and
processing defaults in addition to the final applied processing options.
Treat row-level reports, processing manifests, and retry manifests as sensitive
because they include filenames, relative paths, hashes, and per-file metrics.
Processing plans are also sensitive local evidence for the same reason and are
intended for operator review before running `--process-out`.
`processing_audit_summary.json` is aggregate-only: it records counts, operation
flags, worker metadata, timing, throughput, failure totals, resume counts,
guardrail totals, and max/average/distribution metrics for size change, pixel
change, brightness/contrast delta, crop ratio, dark-border trim margin, deskew
angle, despeckle pixel ratio, opt-in tone-normalization deltas, opt-in
edge-shadow deltas, and opt-in background-stain deltas/change ratios. When
`--despeckle` is enabled, the aggregate
timing block also reports count-only backend mode fields for the optional NumPy
candidate filter or the Python/Pillow fallback; fallback is expected and
non-blocking when NumPy is unavailable. It does not include file lists, paths,
hashes, thumbnails, or image content.
`processing_quality_summary.json` is the public-safe quality-baseline companion
artifact. It reduces the manifest and audit summary to aggregate before/after
quality signals, changed-file counts, metric averages/maxima, guardrails, and
privacy flags. Use it when validating whether synthetic or private aggregate
runs actually improved quality without sharing manifests, filenames, paths,
hashes, thumbnails, OCR text, or image content. Its `quality_signal.status`
distinguishes `measured_with_changes`, `measured_no_quality_operations`, and
`not_applicable`; use the no-changes status as a triage signal, not as a private
evidence leak.
`--normalize-tones` is default-off and should only be enabled for batches with
neutral gray, dark low-contrast text pages, or neutral light-paper low-contrast
text pages. It no-ops when it detects normal exposure, obvious color content,
red stamps, light color annotations, faint marks, dense foreground, too little
tonal separation, or a clear edge-shadow case that should be handled by local
shadow cleanup. Review `tone_normalized_files`, tone delta metrics, and
row-level local reasons before accepting derivatives for archival packages.
When `--deskew --trim-dark-border --auto-crop --despeckle --normalize-tones
--lighten-edge-shadow --lighten-background-stains` are combined, size change,
crop ratio, trim margin, deskew angle, despeckle pixel ratio, bounded tone
deltas, and aggregate same-size pixel-change ratios remain the local guardrails
for explained edits. Pixel-change ratio is still reported for aggregate review,
and remains directly applied to same-size background stain and edge-shadow
changes. For geometric or tone-normalization changes it is deferred to the
size/crop/trim/deskew/tone guardrails so conservative, auditable edits are not
rejected solely because the derivative must be resized or tonally remapped for
comparison. The audit summary records aggregate counts for files where the
pixel guardrail applied directly and files where it was deferred.
Private integration and aggregate baseline summaries also promote the
despeckle backend capability as public-safe aggregate fields:
`requested_backend`, `effective_backend_mode`, `numpy_available`,
`backend_counts`, `fallback_count`, `requested_numpy_fallback_count`, and
warning codes. Treat `--despeckle-backend numpy` with
`despeckle_numpy_unavailable_fallback` or
`despeckle_numpy_requested_all_fallback` as fallback safety validation, not
NumPy performance validation. Actual NumPy performance evidence requires
`requested_backend=numpy`, `numpy_available=true`, and non-zero
`backend_counts.numpy` in the aggregate summary.
`benchmark_results.json` processing runs include a public-safe
`quality_regression` block for automatic retouch combinations. Use it to compare
the default processing path, the base `deskew/trim-dark-border/auto-crop/despeckle`
path, and the full conservative path with `normalize-tones`,
`lighten-edge-shadow`, `lighten-background-stains`, and `lighten-scanlines`.
The block records aggregate status, failure counts, guardrail failed files,
per-operation timing, changed-file counts, conservative thresholds, and
max/average delta or changed-ratio metrics only. A public PR or Linear summary
may cite those aggregate fields and throughput deltas, but must not include
processing manifests, filenames, paths, hashes, OCR, thumbnails, image content,
or per-file reasons.
`--trim-dark-border` is intentionally conservative: automatic trimming requires
dark-edge evidence on all four sides with roughly balanced margins. One-sided,
strongly uneven, low-confidence, or original archival dark edges remain no-op
and record a `dark_border_reason` in the sensitive local
`processing_manifest.json` for operator review.
`run_plan_summary.json` and `run_plan_summary.csv` are also aggregate-only:
they record batch totals, passed and failed counts, P0/P1/P2 counts,
processing failure counts, preflight error counts, throughput aggregates, and
failed batch IDs. They intentionally omit source filenames, source paths,
hashes, thumbnails, row-level file metadata, and image content. Keep the run
plan file itself local if its paths reveal private collection locations.

## Static Workbench Validation

Use `docs/frontend-workbench-prototype.html` for static review of existing
public-safe aggregate artifacts. The workbench can load aggregate summaries such
as `run_plan_summary.json`, `review_summary.json`,
`acceptance_summary.json`, `aggregate_evidence_bundle_summary.json`,
`review_decision_verification_summary.json`,
`final_production_handoff_summary.json`, and `release_candidate_summary.json`.
It can also parse a scan report for local operator review, but scan reports,
review templates, reviewer notes, source images, derivative images, object URLs,
and processing manifests are sensitive local evidence unless they have been
separately reduced to a public-safe aggregate summary.

The public-safe demo fixture gallery in the workbench is the first validation
path for frontend-only changes. Open the HTML file locally, choose a fixture
from the gallery, and select **Load Demo Fixture**. Use the fixtures to confirm
the aggregate summary view, review-decision import/export, readiness checklist,
compatibility diagnostics, and privacy diagnostics without loading private
evidence.

Run the static validator before publishing workbench changes:

```bash
python3 scripts/validate_frontend_workbench.py
python3 scripts/validate_frontend_workbench.py --json
python3 scripts/validate_frontend_workbench.py --json-out /tmp/frontend-workbench-validation.json
python3 scripts/validate_frontend_workbench.py --self-test-json
```

Default mode prints the validated HTML path and exits non-zero on failures.
`--json` prints a deterministic public-safe summary to stdout. `--json-out`
writes the same summary to the requested file. `--self-test-json` checks both
the current success summary and a synthetic missing-workbench failure summary
so orchestration can depend on the JSON shape.

The JSON summary is intentionally high level:

- `status`, `error_count`, and `errors` report pass/fail state and diagnostic
  messages.
- `validated_html_path` is repo-relative when possible.
- `counts` records how many required regions, strings, fields, fixture labels,
  and forbidden-field checks the validator expected.
- `coverage` reports whether aggregate summary, review/acceptance,
  compatibility diagnostics, readiness checklist, demo fixtures, and executable
  fixture checks were covered.
- `fixture_groups` records the expected public-safe fixture coverage.
- `privacy` reports whether forbidden pattern and forbidden field checks passed
  for the page, review import/export summaries, aggregate payloads, and demo
  fixtures.

Keep workbench validation evidence public-safe. Do not include private
filenames, paths, hashes, OCR text, thumbnails, image content, manifests,
row-level findings, reviewer notes, derivative image references, or local
preview object URLs in documentation, PRs, Linear comments, demo fixtures, JSON
summaries, screenshots, or exported review summaries. For static frontend or
validator-only changes, private-image validation on `puersai-hpc` is not
required unless the change also modifies scan processing, production validation
semantics, model inference, private-image behavior, or other runtime behavior
that depends on private images.

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
`skipped_due_to_resume`, `reprocessed_files`, `failed_files`,
`retry_list_files`, `processing_warning_files`, `guardrail_failed_files`, and
the metric maxima. Treat non-zero guardrail failures, unusually high
`pixel_change_ratio` or `size_change_ratio` maxima, or distribution movement
into the highest buckets as over-processing risk that needs local row-level
review of `processing_manifest.json` before derivatives are accepted. Use
`processing_retry_manifest.json` only inside the approved private environment to
identify remaining failed files for another fix-and-resume cycle.

For manual derivative acceptance review, generate the sensitive local package:

```bash
archive-scan-qc processing-review-package \
  --manifest /approved-work/processed-derivatives/batch-001/processing_manifest.json \
  --out /approved-work/processed-derivatives/batch-001/local-review
```

The command writes `processing_review_package.json` and standalone
`processing_review_package.html`. The package groups deskewed,
dark-border-trimmed, cropped, despeckled, background cleanup, readability
improvement, defect cleanup, original appearance risk, failed, and
guardrail-warning records and preserves per-operation decisions for local
review. It may include row-level source/output relative paths, hashes, warnings,
failure reasons, and local derivative links under the operator processing output
tree. Keep it inside the approved environment. It is sensitive local evidence,
not public aggregate evidence, and it must not replace `review_summary.json`,
`processing_audit_summary.json`, or `acceptance_summary.json` in release
materials. The HTML does not embed base64 image data.

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

Use `examples/local_analysis_provider.py` to validate the contract with
synthetic data before approving a real provider:

```bash
archive-scan-qc \
  --input /path/to/synthetic-images \
  --out /path/to/qc-report \
  --analysis-provider-command 'python3 examples/local_analysis_provider.py'
```

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
synthetic providers before enabling a real local model.

For future PaddleOCR, ONNX, Paddle, or GPU-capable providers, keep the scanner
contract unchanged and put model-specific setup behind the local provider
command. A GPU sidecar can run on the same approved workstation or secured
processing node, but it must stay offline/local and must receive work only
through the JSONL request records or local file paths already present on that
machine. Do not adapt the scanner for cloud queues, remote inference endpoints,
platform-specific GPU orchestration, or direct model dependencies.

Forbidden provider outputs include OCR transcripts, detected personal data,
image bytes, thumbnails, crops, base64 payloads, embeddings, prompts, raw model
logs, absolute paths, hashes, private filenames, and any cloud or network
location. Provider stdout should contain only the documented metadata and
finding records, and metadata should remain aggregate or model/run-level.

## Capability Probe

Run the optional capability probe when validating whether a host has local
GPU/model readiness for future providers:

```bash
archive-scan-qc capability-probe \
  --out /approved-work/validation/capability-probe
```

The probe is informational and non-blocking. It reports aggregate package
availability, aggregate GPU visibility, and whether GPU/model acceleration has
been configured. It does not open private images, execute provider commands, run
model inference, add dependencies, or contact network services.

Keep it disabled by default by omitting `--analysis-provider-command` from scan
runs and leaving acceleration environment flags unset. A missing optional
package or invisible GPU should be interpreted as "future acceleration not
ready", not as a failure of the required CPU/Pillow production baseline. Report
only the aggregate fields from `capability_probe.json`: package labels,
provider/GPU counts, configured booleans, warnings, and the
`unchanged_cpu_pillow_baseline` semantics marker.

## Public Capability Contract

Run the public capability contract command during release validation and when a
production operator needs to confirm which CLI surfaces and processing backends
are stable:

```bash
archive-scan-qc public-capability-contract \
  --out /approved-work/validation/public-capability-contract
```

The command writes `public_capability_contract.json` with schema
`scan-qc.public-capability-contract.v1`. It is public-safe and aggregate-only:
it does not scan directories, open images, run image processing, execute
provider commands, probe hardware, read environment values, or include local
paths, filenames, hashes, OCR text, thumbnails, or image content.

Use this contract as the current boundary between stable CLI behavior and
internal or experimental implementation paths. The required baseline remains
CPU/Pillow. `--despeckle-backend fallback` and optional
`--despeckle-backend numpy` are stable public CLI backends. OpenCV despeckle,
libvips image IO, and built-in GPU/model inference are not stable public CLI
capabilities unless a release updates the contract, README, and this runbook
together.

The contract is also the source of truth for artifact sharing. Public-safe
aggregate artifacts include validation indexes, aggregate evidence bundles,
final handoff summaries, processing audit summaries, rule-template catalog and
dry-run summaries, benchmark/capability summaries, and workbench public
summaries after local policy review.
`service_job_public_summary.json` is included only as prototype/validation
evidence for the service job boundary core until the HTTP/API surface is
promoted.
Path-bearing local operational files such as `production_run_summary.json`,
`production_run_progress.json`, scan reports, processing manifests, review
templates, rework lists, production review queues, and delivery manifests stay
local-only unless reduced by a public-safe aggregate command.

## Image Processing Capability Smoke

Run the synthetic image-processing capability smoke during release validation
or after installing on a production workstation when private test images are not
approved for sharing:

```bash
archive-scan-qc image-processing-capability-smoke \
  --out /approved-work/validation/image-processing-capability-smoke
```

The command generates synthetic images in a temporary directory, runs the real
scan and derivative-processing path, and writes
`image_processing_capability_smoke.json` with schema
`scan-qc.image-processing-capability-smoke.v1` plus
`processing_quality_summary.json` with schema
`scan-qc.processing-quality-summary.v1`. Use these as aggregate evidence that
source images remain unmodified, derivative processing executes, guardrail
failures are zero or explained, requested stable despeckle backend is
represented in backend counts, and before/after quality signals are measurable.
The smoke status fails with explicit blocking codes if any declared required
quality operation has zero applied files in the aggregate audit counts.
The smoke also requires one synthetic small-angle deskew and publishes aggregate
`deskew_abs_angle_degrees` evidence. It checks that guarded tone normalization
improves at least one neutral light-paper low-contrast fixture, and that paper
color-cast normalization, edge-shadow cleanup, corner-shadow cleanup, localized
background-stain cleanup, fold-shadow cleanup, illumination-gradient leveling,
conservative diffuse bleed-through cleanup, broad thin-paper bleed-through
cleanup, and scanline lightening each produce at least one public-safe aggregate
delta. It also requires faded-text enhancement and text-edge sharpening evidence
from a synthetic mildly blurred typed-text fixture, without exposing image
content.
Both JSON files are public-safe aggregate evidence and must not contain paths,
filenames, hashes, OCR text, thumbnails, or image content.

## 本地生产工作台入口和就绪检查

本地生产入口面向单台扫描处理电脑，不需要打包、安装器、后台服务或局域网监控。
班次开始前，先在仓库根目录运行推荐的就绪门禁：

```bash
npm run smoke:production-workbench-suite
```

门禁通过后，启动中文生产工作台：

```bash
archive-scan-qc production-workbench
```

浏览器打开后，扫描操作员按页面顺序完成本地流程：

1. 选择原图文件夹和输出文件夹，确认文件夹保存成功。
2. 点击“开始处理”，等待进度区显示本批次处理状态。
3. 在大图预览区检查原图和处理后图片，按提示查看需要确认的图片。
4. 对每张待确认图片选择处理决定：通过、重扫、重新处理、保留原样或跳过。
5. 处理和复核完成后，从完成/导出入口保存本批次复核决定和生产结果。

工作台只处理本机选择的文件夹和本机输出目录。不要把私有文件名、本地路径、
哈希、OCR 文本、缩略图、行级发现或图片内容发布到 Linear、GitHub 或外部报告。

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

Before review is finalized into aggregate acceptance evidence, create the
local-only operator rework queue and then write the review summary:

```bash
archive-scan-qc rework-action-list \
  --report /approved-work/qc-reports/batch-001/scan_qc_report.json \
  --processing-audit-summary /approved-work/processed-derivatives/batch-001/processing_audit_summary.json \
  --processing-retry-manifest /approved-work/processed-derivatives/batch-001/processing_retry_manifest.json \
  --out /approved-work/qc-reports/batch-001/rework_action_list.json \
  --csv-out /approved-work/qc-reports/batch-001/rework_action_list.csv

archive-scan-qc review-summary \
  --review /approved-work/qc-reports/batch-001/review_template.csv \
  --out /approved-work/qc-reports/batch-001/review_summary.json
```

Run `archive-scan-qc rework-action-list` after automated QC and any derivative
processing/retry evidence, and before closing the review template into
`review_summary.json`. The JSON/CSV outputs are operator work queues, not public
release artifacts. They group row-level findings into rescan required,
reprocess candidate, manual review, duplicate/manifest correction, processing
retry, and informational follow-up actions. They are clearly marked local-only
sensitive evidence because they include paths, hashes, row-level messages, and
processing errors; they do not embed thumbnails, image bytes, or source images.

For the redesigned Chinese production workbench, generate the focused local
operator queue from the same private artifacts:

```bash
archive-scan-qc production-review-queue \
  --scan-qc-report /approved-work/qc-reports/batch-001/scan_qc_report.json \
  --processing-review-package /approved-work/processed-derivatives/batch-001/local-review/processing_review_package.json \
  --rework-action-list /approved-work/qc-reports/batch-001/rework_action_list.json \
  --out /approved-work/qc-reports/batch-001/production_review_queue.json
```

`production_review_queue.json` contains Chinese reasons, stable local queue
IDs, severity, source category, suggested operator actions (`pass`, `rescan`,
`reprocess`, `keep_original_trace`, or `skip`), and local-only sensitivity
metadata. It is for the approved local production environment only and does not
embed image bytes, thumbnails, base64 data, OCR text, or hashes.

`review_summary.json` includes severity, rule, status, severity/status, and
rule/status counts, remaining P0/P1 counts, and `acceptance_passed`. It contains
no row-level path list, filenames, hashes, messages, or reviewer notes. P0/P1
findings remain open unless their status is `fixed` or `false_positive`.
Acceptance passes only when remaining P0 and remaining P1 counts are both zero.

## Production Acceptance Gate

Run the final production gate after batch execution, derivative processing,
human review, and any approved benchmark run have produced aggregate evidence.
The command accepts any combination of aggregate evidence, but missing evidence
is reported as a warning and at least one input is required.

```bash
archive-scan-qc acceptance-summary \
  --run-plan-summary /approved-work/project/run_plan_summary.json \
  --review-summary /approved-work/project/review_summary.json \
  --processing-audit-summary /approved-work/project/processing_audit_summary.json \
  --benchmark-results /approved-work/project/benchmark_results.json \
  --min-scan-files-per-minute 100 \
  --min-processing-files-per-minute 60 \
  --out /approved-work/project/acceptance_summary.json
```

Default blocking criteria are strict for final delivery: remaining P0 must be
zero, remaining P1 must be zero, failed batches must be zero, and processing
failed files must be zero. Optional throughput thresholds block acceptance when
provided evidence is below the configured minimum. If a threshold is configured
but no matching throughput evidence is supplied, the command emits a warning.

`acceptance_summary.json` is the shareable aggregate acceptance conclusion after
local policy review. It includes pass/fail status, blocking items, warnings,
remaining P0/P1 counts, failed batch and processing failure counts, throughput
and worker summaries, human review status, and recommended next steps. It must
not include source filenames, source locations, hashes, thumbnails, row-level
findings, reviewer notes, OCR/text, or image content.

## Aggregate Review-Decision Handoff

After local human review decisions are exported, use the aggregate-only handoff
sequence below to verify final review decisions and production readiness without
reading implementation PRs or exposing local evidence:

```bash
archive-scan-qc review-decisions-verify \
  --summary /placeholder/private-review-decisions/review_decisions.json \
  --out /placeholder/private-validation-output/review_decision_verification_summary.json

archive-scan-qc private-validation-aggregate \
  --input-dir /placeholder/private-validation-output/private-results \
  --out /placeholder/private-validation-output

archive-scan-qc evidence-bundle-verify \
  --evidence-dir /placeholder/private-validation-output

archive-scan-qc final-handoff-summary \
  --evidence-dir /placeholder/private-validation-output

archive-scan-qc public-safe-validation-index \
  --input-dir /placeholder/private-validation-output

archive-scan-qc artifact-readiness-checklist \
  --evidence-dir /placeholder/private-validation-output

PYTHONPATH=src python3 scripts/validate_frontend_workbench.py \
  --json-out /placeholder/private-validation-output/frontend_workbench_validation.json
```

Optional inspection can then happen in the static workbench by loading
`final_production_handoff_summary.json`,
`artifact_readiness_checklist.json`, `frontend_workbench_validation.json`, or
`workbench_public_summary.json`. The workbench displays only aggregate status,
readiness, compatibility diagnostics, validation coverage, privacy diagnostics,
and code/count summaries; it must not be used to publish loaded private files,
local preview state, object URLs, manifests, derivative image references,
command lines, environment values, or row-level evidence.

`review_decision_verification_summary.json` is designed to contribute aggregate
decision counts, privacy status, blocking and warning counts by code, and final
handoff readiness. It must not include private filenames, source roots, hashes,
OCR text, thumbnails, row-level findings, reviewer notes, prompts, provider
commands, raw model output, object URLs, or actual sample data.

`private_validation_aggregate_summary.json` is designed for operator-approved
private image-quality validation. Raw private validation rows remain local; the
published artifact may include only public group IDs, aggregate item counts,
allowlisted metric summaries, quality-signal status counts, risk-code counts,
and privacy self-check status. It must not include private labels, local paths,
filenames, hashes, OCR text, thumbnails, row-level rows, or image bytes.

`aggregate_evidence_bundle_summary.json` and
`final_production_handoff_summary.json` are public-safe aggregate summaries
after local policy review. `artifact_readiness_checklist.json` is also
public-safe after local policy review when generated from aggregate fixtures or
approved aggregate outputs only. It records expected artifact rows, present and
missing counts, pass/fail readiness, aggregate blocking/warning code counts,
and privacy indicators. `frontend_workbench_validation.json` is public-safe
aggregate validation evidence when generated by the static workbench validator
against the checked-in workbench and reviewed locally; it records validator
status, aggregate coverage counts, fixture groups, and privacy-check outcomes
only. Share these artifacts when operators need artifact-level readiness or
static workbench validation for a release/workbench handoff; otherwise the
concise final handoff summary is enough. Local/source artifacts remain
sensitive, including source and
derivative images, scan reports, scan CSV/HTML exports, review templates,
reviewer notes, processing manifests, retry manifests, delivery manifest rows
for sensitive evidence, provider logs, provider commands, prompts, raw model
output, embeddings, command lines, environment values, network/cloud locations,
local preview
state, and any artifact that contains row-level paths, hashes, messages, or
local roots.

## Delivery Handoff Manifest

After the acceptance gate is complete, create a local delivery handoff manifest
for evidence review:

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

The command writes `delivery_handoff_manifest.json` and
`delivery_handoff_manifest.csv`. Both outputs contain only manifest metadata:
role, local path, filename, byte size, SHA-256, detected schema version, and
sensitivity classification. The command rejects missing artifacts and does not
copy source images, derivative images, reports, manifests, or any other input
file. It performs no upload.

Treat `review_summary.json`, `processing_audit_summary.json`,
`benchmark_results.json`, `acceptance_summary.json`, and other known aggregate
summary schemas as `aggregate_public_safe` after local policy review. Treat
`scan_qc_report.json`, scan report CSV/HTML exports, review templates,
`processing_manifest.json`, `processing_retry_manifest.json`, processing review
packages, and unknown extra artifacts as `sensitive_local_evidence`.

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

## Acceptance Sampling Export

For manual acceptance sampling, generate a local review list after automated QC:

```bash
archive-scan-qc acceptance-sampling-export \
  --report /approved-work/qc-reports/batch-001/scan_qc_report.json \
  --out /approved-work/qc-reports/batch-001/acceptance-sampling
```

The command writes `acceptance_sampling_review.json` and
`acceptance_sampling_review.csv`. The default deterministic sample is at least
5% of scan records. P0, P1, P2, and other problematic records are prioritized
before baseline records when practical, matching the manual-inspection
expectation that high-risk automated findings receive operator attention.

Treat both files as sensitive local evidence. They include row-level local
evidence fields such as relative paths, hashes, dimensions, DPI, automated rule
ids, and blank reviewer status fields. They must not include embedded images,
thumbnails, OCR text, recognized text, or image bytes. The JSON includes an
`aggregate_sampling_counts` block suitable for acceptance notes. That aggregate
block reports the input total, target sampling ratio, target sample count,
generated sample-task count, reviewed sample-task count, and whether reviewed
samples meet the current target without publishing row-level evidence.

Sensitive local evidence includes source images, derivative images,
`scan_qc_report.json`, `scan_qc_report.html`, `scan_qc_files.csv`,
`scan_qc_findings.csv`, `processing_manifest.json`, processing review packages,
and review templates. Keep these inside the approved environment.
`review_summary.json` is designed as the shareable aggregate acceptance
evidence, but still review it for local policy before external release.

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
processing, aggregate benchmark, aggregate acceptance, delivery handoff
manifest, an isolated package install, `archive-scan-qc --version`, and a
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
- `archive-scan-qc processing-plan`: writes local dry-run JSON/CSV for operator
  review without derivative images.
- `--auto-crop`: conservative page-border crop. It only accepts consistent
  four-side page evidence and leaves blank, low-contrast, near-full-frame,
  edge-content, sparse-noise, or excessive-crop candidates unchanged with a
  local manifest reason for review.
- `--deskew`: conservative small-angle deskew. Pages with insufficient
  foreground, low confidence, excessive angle, inconsistent line evidence, or
  foreground touching a rotation boundary are left unchanged with a local
  manifest reason for review.
- `--trim-dark-border`: dark edge trim before crop.
- `--despeckle`: isolated dark speckle cleanup.
- `--lighten-edge-shadow`: default-off narrow neutral page-edge shadow
  lightening with content, color, binding, and broad-lighting no-op guards.
- `--lighten-background-stains`: default-off small neutral light background
  stain lightening with text, stamp, annotation, binding, color, historical
  damage, broad-lighting, and low-confidence no-op guards.
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
