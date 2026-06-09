# Release checklist

Run this checklist before tagging or publishing an `ai4archive` package build.

## Required validation

- Run `PYTHONPATH=src python3 -m unittest discover -s tests`.
- Run `PYTHONPATH=src python3 -m compileall -q src tests`.
- Run `PYTHONPATH=src:tests python3 scripts/ci_regression_groups.py
  verify-coverage` and confirm every `tests/test_*.py` module is assigned to
  exactly one of `core-image-processing`, `production-cli`,
  `privacy-boundary`, or `external-validation`.
- Run `PYTHONPATH=src python3 scripts/check_offline_dependencies.py`.
- Run `python3 scripts/validate_release.py`.
- Confirm `archive-scan-qc --version` matches the package version.
- Confirm `archive-scan-qc public-capability-contract --out
  /placeholder/private-validation-output/public-capability-contract` creates
  `public_capability_contract.json` with schema
  `scan-qc.public-capability-contract.v1`, classifies stable CLI surfaces and
  artifact schemas, and keeps OpenCV/libvips/GPU model inference out of the
  stable public CLI backend list unless intentionally promoted in this release.
- Confirm the public capability contract keeps path-bearing local operational
  outputs such as `production_run_summary.json`,
  `production_run_progress.json`, scan reports, processing manifests, review
  templates, rework lists, production review queues, and delivery manifests
  classified as local-sensitive unless a separate aggregate command reduces
  them to a public-safe artifact.
- Confirm service-job boundary tests create isolated per-job `metadata`,
  `derivatives`, `tmp`, `checkpoints`, and `logs` directories, reject input and
  service-root overlap, recover stale `running` progress as `needs_recovery`,
  and keep `service_job_public_summary.json` free of paths, filenames, hashes,
  OCR text, thumbnails, and image content. Treat `service_job.json` as private
  checkpoint state.
- Confirm `archive-scan-qc image-processing-capability-smoke --out
  /placeholder/private-validation-output/image-processing-capability-smoke`
  creates `image_processing_capability_smoke.json` with schema
  `scan-qc.image-processing-capability-smoke.v1`, runs synthetic scan and
  derivative processing, reports `source_images_modified=false`, writes
  `processing_quality_summary.json` with schema
  `scan-qc.processing-quality-summary.v1`, reports a passing public-safe
  quality baseline with tone, fold-shadow, bleed-through, scanline, faded-text,
  and text-edge aggregate deltas from synthetic fixtures, and contains no paths,
  filenames, hashes, OCR text, thumbnails, or image content.
- Confirm `archive-scan-qc rule-template-catalog --out
  /placeholder/private-validation-output/rule-template-catalog` and
  `archive-scan-qc rule-template-dry-run --rule-template text-clean-print --out
  /placeholder/private-validation-output/rule-template-dry-run` create
  `rule_template_catalog.json` and `rule_template_dry_run.json` with schemas
  `scan-qc.rule-template-catalog.v1` and `scan-qc.rule-template-dry-run.v1`,
  report `Derivative images written: no`, and contain no paths, filenames,
  hashes, OCR text, thumbnails, image content, or row-level evidence.
- Confirm the validator's examples-based dry-run created `preflight_report.json`,
  JSON, HTML, CSV, a processing manifest, a processing retry manifest, an
  aggregate processing audit summary, and derivative images from synthetic
  temporary inputs.
- Confirm a synthetic review template can be exported and a privacy-safe
  aggregate `review_summary.json` can be generated with no remaining P0/P1
  findings before acceptance.
- Confirm a synthetic `archive-scan-qc calibrate-rules` run creates
  `rules_calibration_summary.json` after automated QC and review summary, and
  that any `--write-suggested-profile` output is marked draft/suggested and
  does not overwrite the original profile.
- Confirm a synthetic multi-batch `archive-scan-qc run-plan` creates per-batch
  preflight/scan/processing artifacts plus aggregate `run_plan_summary.json`
  and `run_plan_summary.csv`.
- Confirm a synthetic `archive-scan-qc production-run --rule-template
  text-clean-print` creates `production_run_summary.json`,
  `production_run_progress.json`, derivative images, admin reports, and records
  the rule template ID plus applied template processing defaults, including
  `despeckle_content_type_check=false` for confirmed pure-text cleanup runs.
  Confirm `processing_manifest.json` also records the sanitized `rule_template`
  snapshot with template ID, version, source, and processing defaults.
- For external CLI batch-service regression on approved DIBCO/H-DIBCO samples,
  run `python scripts/run_dibco_external_cli_test.py --data-root
  D:\data-opt\DIBCO-H-DIBCO` from an environment with package requirements plus
  `numpy`, then confirm `stable_cli_passed=true`, zero source-image
  modifications, zero missing processed outputs, and review the generated
  quality/performance report.
- For CI-safe external validation smoke, confirm
  `PYTHONPATH=src:tests python3 scripts/ci_regression_groups.py run
  external-validation` uses only synthetic DIBCO/NoisyOffice-shaped data in a
  temporary output root; treat it as CLI integration coverage, not benchmark
  evidence.
- For document-cleanup regression aligned with the current non-binarization
  image-processing scope, run `python scripts/run_noisyoffice_external_cli_test.py
  --data-root D:\data-opt\NoisyOffice` from an environment with package
  requirements plus `numpy`, then confirm `stable_cli_passed=true`, zero
  source-image modifications, zero missing processed outputs, zero size
  mismatches, and review PSNR/SSIM/MAE deltas against clean grayscale GT.
- Confirm an invalid or interrupted synthetic `production-run` writes a terminal
  `failed` or `interrupted` progress state and a recovery-oriented production
  summary instead of leaving the batch indefinitely `running`.
- Confirm a synthetic `archive-scan-qc acceptance-summary` run creates an
  aggregate `acceptance_summary.json` with pass/fail status, blocking items,
  warnings, P0/P1 remaining counts, processing failure count, throughput and
  worker summaries, human review status, and recommended next steps.
- Confirm a synthetic `archive-scan-qc review-decisions-verify` run creates
  `review_decision_verification_summary.json` with aggregate decision counts,
  privacy status, blocking/warning counts by code, and final handoff readiness,
  without private filenames, roots, hashes, OCR text, thumbnails, row-level
  findings, reviewer notes, prompts, provider commands, raw model output,
  object URLs, or sample data.
- Confirm `PYTHONPATH=src python3 scripts/validate_frontend_workbench.py
  --json-out /placeholder/private-validation-output/release-candidate/frontend_workbench_validation.json`
  creates a public-safe aggregate frontend workbench validation summary with
  pass/fail status, coverage counts, fixture groups, and privacy-check outcomes
  only.
- Confirm CI is green for Python 3.10, 3.11, and 3.12.

## Package and install checks

- Confirm `pyproject.toml` metadata, Python version constraint, Pillow range,
  license, README reference, and console script are correct.
- Install the built wheel in a clean virtual environment.
- Run a synthetic image smoke scan and confirm JSON, HTML, files CSV, and
  findings CSV are created.
- Confirm `scan_qc_report.json` contains `rule_catalog` and the HTML report
  contains a Rule Catalog section.
- Parse `examples/rules-profile.production-sample.json` and
  `examples/manifest.sample.csv` through `archive-scan-qc preflight` and the
  scan/process CLI against synthetic files named `BATCH001_PAGE_0001.png` and
  `BATCH001_PAGE_0002.png`.
- Confirm `archive-scan-qc preflight`, `production-run`, and `run-plan` accept
  built-in rule template IDs `dat-31-2017-standard`, `text-clean-print`, and
  `high-fidelity-original`, and reject `--rule-template custom` without
  `--rules-profile`. Confirm `text-clean-print` records the disabled
  despeckle content-type preservation gate in processing manifests and
  production-run options.
- Confirm `archive-scan-qc rule-template-dry-run --rule-template custom`
  rejects the request with a public-safe operator error unless a future
  validated custom-template dry-run path is added.
- Run the `examples/local_analysis_provider.py` smoke test with
  `--analysis-provider-command` and confirm exactly one `source=provider`
  finding is reported, provider metadata is sanitized, and omitting the flag
  preserves the default rules-only path.
- For offline or domestic-platform deployments, install from the approved local
  wheelhouse and verify the Pillow wheel on target hardware.
- Before isolated production validation, run the read-only offline dependency
  check against the approved wheelhouse:

  ```bash
  PYTHONPATH=src python3 scripts/check_offline_dependencies.py \
    --wheelhouse /mnt/d/ai4archive-wheelhouse
  ```

  The command does not install packages or use the network. It reports only
  aggregate Python/package versions and wheel counts by package name. If the
  release build has not yet been copied into the wheelhouse, rerun with
  `--wheelhouse-warning-only` to collect warnings without blocking local smoke
  checks.

## Operations readiness

- Review `docs/operations-runbook.md`.
- Review `docs/standards-traceability.md`.
- Confirm `src/archive_scan_qc/rule_registry.py`,
  `docs/standards-traceability.md`, README, and the runbook describe the same
  DA/T 31-2017 rule mapping and do not include private filenames, paths,
  hashes, thumbnails, OCR text, or image content.
- Confirm the runbook's install, offline wheelhouse, domestic-platform,
  resource sizing, directory, report archival, exit-code, privacy,
  troubleshooting, and performance tuning guidance still matches the release.
- Confirm README, `docs/public-capability-contract.md`, and the runbook describe
  the same stable public CLI, schema, and processing backend boundary.
- Confirm public PRs, issues, and release notes reference only synthetic
  examples or aggregate benchmark output.
- Confirm production runbooks call `archive-scan-qc preflight` before the full
  scan/process command and explain preflight error versus warning semantics.
- Confirm production runbooks explain `--resume-processing`,
  `processing_audit_summary.json`, and the private
  `processing_retry_manifest.json` retry workflow for interrupted batches.
- Confirm derivative-processing release evidence includes aggregate audit
  review: zero or explained `guardrail_failed_files`, acceptable
  `processing_warning_files`, and stable max/average/distribution values for
  size, pixel, tonal, crop, trim, deskew, and despeckle changes.
- Confirm production runbooks explain `archive-scan-qc run-plan`,
  `--continue-on-error`, failed batch IDs, resume-processing fields in the
  plan, and the privacy boundary between aggregate project summaries and
  sensitive batch-level reports.
- Confirm production runbooks explain the manual review template, allowed
  disposition statuses, aggregate acceptance summary, and which review artifacts
  are sensitive local evidence.
- Confirm production runbooks explain final acceptance gate defaults, optional
  scan and processing throughput thresholds, missing-evidence warnings, and the
  privacy boundary for `acceptance_summary.json`.
- Confirm production runbooks explain the aggregate-only final review-decision
  handoff sequence: `review-decisions-verify`, `evidence-bundle-verify`,
  `final-handoff-summary`, then optional static workbench inspection of
  `final_production_handoff_summary.json`.
- Confirm production runbooks distinguish public-safe aggregate summaries
  (`review_decision_verification_summary.json`,
  `aggregate_evidence_bundle_summary.json`, and
  `final_production_handoff_summary.json` after local policy review) from
  sensitive local/source artifacts such as source images, derivative images,
  scan reports, review templates, reviewer notes, processing manifests, retry
  manifests, provider logs, object URLs, and row-level paths or hashes.
- Confirm README and runbook document the offline provider JSONL contract,
  `provider.<name>.<rule>` namespace, protected built-in P0 rule boundary,
  provider disable path, local/GPU sidecar guidance for future PaddleOCR/ONNX/
  Paddle providers, and the prohibition on uploads, image bytes, thumbnails,
  OCR text, paths, hashes, filenames, raw model logs, and file content.
- Confirm production runbooks explain the threshold calibration loop:
  automated QC, human review, aggregate calibration recommendation, then human
  approval before changing a project rules profile.

## Real sample aggregate validation

- For one-command `puersai-hpc` production validation, run the aggregate wrapper
  with placeholder-equivalent private paths and configured thresholds:

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
    --min-processing-files-per-minute 80
  ```

  Use the fixed 20-image sample for low-risk tooling validation and the fixed
  149-image sample for full production validation. The wrapper must leave only
  aggregate public evidence files in the validation output root:
  `aggregate_baseline_summary.json` and `acceptance_summary.json`.
- Run `archive-scan-qc benchmark` on approved internal real samples.
- Share only aggregate `benchmark_results.json` or `benchmark_results.csv`.
- For optimized scan QC baseline release checks, validate the public
  `aggregate_baseline_summary.json` with `archive-scan-qc acceptance-summary`
  using configured scan and processing throughput thresholds. Confirm the
  baseline privacy self-check passes and cleanup retained only
  `aggregate_baseline_summary.json`.
- Build the aggregate release-candidate decision artifact from the production
  validation summaries and release readiness summary:

  ```bash
  PYTHONPATH=src python3 scripts/release_candidate_summary.py \
    --input /placeholder/private-20-image-sample \
    --out /placeholder/private-validation-output/release-candidate \
    --release-readiness-summary /placeholder/private-validation-output/readiness/release_readiness_summary.json \
    --workers 4 \
    --benchmark-workers-list 1,2,4,8 \
    --process-images \
    --auto-crop \
    --deskew \
    --trim-dark-border \
    --despeckle \
    --min-scan-files-per-minute 100 \
    --min-processing-files-per-minute 80
  ```

  Share only `release_candidate_summary.json` from this step. It contains
  aggregate production validation status, throughput, failure and severity
  counts, privacy and cleanup status, release readiness status, blocking counts,
  capability-probe counts, and the CPU/Pillow semantics marker.
- Record total files, openable files, finding counts, elapsed seconds, files per
  minute, recommended scan/processing workers, effective workers, OS/platform
  family, Python version family, CPU logical count, total memory GB when
  available, output disk free/total GB, visible GPU count, aggregate GPU memory
  GB when `nvidia-smi` is available, GPU acceleration status, and sanitized
  telemetry warning count.
- Keep normal scan reports, processing manifests, filenames, paths, hashes,
  thumbnails, retry manifests, derivative images, and source images inside the
  approved private environment.
- Generate `archive-scan-qc delivery-manifest` for local handoff review after
  acceptance. Share only entries classified as aggregate/public-safe after local
  policy review; keep sensitive local evidence entries inside the approved
  environment.
- Verify exported local human review decisions with the aggregate-only review
  decision verifier:

  ```bash
  archive-scan-qc review-decisions-verify \
    --summary /placeholder/private-review-decisions/review_decisions.json \
    --out /placeholder/private-validation-output/review_decision_verification_summary.json
  ```

  Share only `review_decision_verification_summary.json` after local policy
  review. It contributes aggregate decision counts, privacy status, blocker and
  warning counts by code, and final handoff readiness; it must not expose
  private filenames, roots, hashes, OCR text, thumbnails, row-level findings,
  reviewer notes, prompts, provider commands, raw model output, object URLs, or
  actual sample data.
- Run the aggregate evidence bundle verifier against the directory that contains
  the release-candidate, readiness, acceptance, provider probe, and production
  validation aggregate summaries, plus review-decision verification when final
  human decisions are part of the handoff:

  ```bash
  archive-scan-qc evidence-bundle-verify \
    --evidence-dir /placeholder/private-validation-output/release-candidate
  ```

  Share only `aggregate_evidence_bundle_summary.json` after it passes. The
  verifier checks JSON parseability, schema/status/count fields, artifact
  presence, and privacy indicators, and reports private evidence findings by
  code without copying private paths, filenames, hashes, OCR text, thumbnails,
  image content, row-level findings, secrets, or sample roots into the summary.
- After the release-candidate summary and aggregate evidence bundle verifier
  have run, generate the final public handoff decision:

  ```bash
  archive-scan-qc final-handoff-summary \
    --evidence-dir /placeholder/private-validation-output/release-candidate
  ```

  Share `final_production_handoff_summary.json` as the concise go/no-go handoff
  status. It consumes only aggregate summaries such as
  `aggregate_evidence_bundle_summary.json` and `release_candidate_summary.json`,
  reports blockers by aggregate code/count, and does not read source images,
  row-level reports, manifests, OCR text, thumbnails, hashes, private roots, or
  derivative images.
- Index approved aggregate artifacts and generate the public-safe artifact
  readiness checklist for operator handoff/workbench inspection:

  ```bash
  archive-scan-qc public-safe-validation-index \
    --input-dir /placeholder/private-validation-output/release-candidate

  archive-scan-qc artifact-readiness-checklist \
    --evidence-dir /placeholder/private-validation-output/release-candidate

  PYTHONPATH=src python3 scripts/validate_frontend_workbench.py \
    --json-out /placeholder/private-validation-output/release-candidate/frontend_workbench_validation.json
  ```

  Share `artifact_readiness_checklist.json` when the release or workbench
  handoff needs artifact-level readiness rows, aggregate missing counts, and
  blocking/warning counts by code. Keep local private artifacts private:
  source images, row-level findings, processing manifests, CSV row reports, OCR
  text, thumbnails, hashes, private filenames or roots, derivative images,
  reviewer notes, provider commands, raw model output, prompts, embeddings,
  environment values, network/cloud locations, and preview state are not
  shareable checklist inputs or outputs.
- Share `frontend_workbench_validation.json` only as aggregate/public-safe
  static workbench validation evidence after local policy review. It is not a
  private evidence package and must not include private filenames, paths,
  images, OCR text, hashes, thumbnails, row-level reports, review notes, object
  URLs, manifests, derivative image references, command lines, or environment
  values.
- Optionally open `docs/frontend-workbench-prototype.html` locally and load
  `final_production_handoff_summary.json`,
  `artifact_readiness_checklist.json`, `frontend_workbench_validation.json`, or
  `workbench_public_summary.json` for static inspection of aggregate readiness,
  validation coverage, privacy diagnostics, and code/count diagnostics. Do not
  load or publish private source artifacts, object URLs, manifests, derivative
  image references, command lines, environment values, row-level findings,
  reviewer notes, prompts, provider commands, raw model output, or actual
  sample data through the workbench.

## Privacy prohibitions

- Do not upload or attach private source images.
- Do not upload or attach derivative images from private collections.
- Do not publish filenames, relative paths, source hashes, thumbnails,
  row-level findings, row-level file metadata, processing manifests, or retry
  manifests from private collections.
- Do not paste standalone HTML or JSON scan reports from private collections
  into public issues, PRs, chats, or release notes.
- Do not upload or attach filled review templates or reviewer notes from
  private collections; share only aggregate `review_summary.json` after policy
  review.
- Do not publish local review-decision exports, reviewer notes, or workbench
  preview state. Share only aggregate
  `review_decision_verification_summary.json` after policy review.
- Do not enable an analysis provider that uploads source images, thumbnails,
  OCR text, derived content, hashes, row-level metadata, or findings to a
  network service.
- Do not upload or attach private `scan_qc_report.json` files for calibration.
  Share only aggregate `rules_calibration_summary.json` after policy review.
- Do not publish run plan files if their input, manifest, rules-profile,
  report, or processing-output paths reveal private collection locations. Share
  only aggregate `run_plan_summary.json` or `run_plan_summary.csv` after policy
  review.
- Do not publish delivery handoff manifest rows classified as
  `sensitive_local_evidence`; they can contain local paths and hashes for
  row-level artifacts.
- Do not publish frontend workbench validation evidence that contains private
  filenames, paths, images, OCR text, hashes, thumbnails, row-level reports,
  review notes, object URLs, manifests, derivative image references, command
  lines, or environment values.

## Performance record

- Record worker settings tested, recommended workers from
  `benchmark_results.json` `recommendations`, effective worker count, elapsed
  seconds, files per minute, processed files per minute when processing is
  enabled, OS/platform family, Python version family, CPU logical count, total
  memory GB when available, output disk free/total GB, visible GPU count,
  aggregate GPU memory GB when available, GPU acceleration status, telemetry
  warning count, and Pillow version.
- Compare against the previous release on the same sample set and hardware.
- Note any throughput regression or operational limit in release notes.

## Rollback

- Keep the previous wheel, release tag, rules profile, and deployment command
  available.
- If a production rollout finds unexpected P0 volume, report schema issues, or
  unacceptable throughput regression, reinstall the previous wheel and rerun the
  same aggregate benchmark and a representative smoke scan.
- Preserve the failed run's aggregate benchmark output and synthetic
  reproduction data for debugging. Do not export private row-level artifacts.
