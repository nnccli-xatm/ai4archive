# Release checklist

Run this checklist before tagging or publishing an `ai4archive` package build.

## Required validation

- Run `PYTHONPATH=src python3 -m unittest discover -s tests`.
- Run `PYTHONPATH=src python3 -m compileall -q src tests`.
- Run `python3 scripts/validate_release.py`.
- Confirm `archive-scan-qc --version` matches the package version.
- Confirm the validator's examples-based dry-run created `preflight_report.json`,
  JSON, HTML, CSV, a processing manifest, and derivative images from synthetic
  temporary inputs.
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
- For offline or domestic-platform deployments, install from the approved local
  wheelhouse and verify the Pillow wheel on target hardware.

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
- Confirm public PRs, issues, and release notes reference only synthetic
  examples or aggregate benchmark output.
- Confirm production runbooks call `archive-scan-qc preflight` before the full
  scan/process command and explain preflight error versus warning semantics.

## Real sample aggregate validation

- Run `archive-scan-qc benchmark` on approved internal real samples.
- Share only aggregate `benchmark_results.json` or `benchmark_results.csv`.
- Record total files, openable files, finding counts, elapsed seconds, files per
  minute, recommended scan/processing workers, effective workers, CPU count,
  platform, and Python version.
- Keep normal scan reports, processing manifests, filenames, paths, hashes,
  thumbnails, derivative images, and source images inside the approved private
  environment.

## Privacy prohibitions

- Do not upload or attach private source images.
- Do not upload or attach derivative images from private collections.
- Do not publish filenames, relative paths, source hashes, thumbnails,
  row-level findings, row-level file metadata, or processing manifests from
  private collections.
- Do not paste standalone HTML or JSON scan reports from private collections
  into public issues, PRs, chats, or release notes.

## Performance record

- Record worker settings tested, recommended workers from
  `benchmark_results.json` `recommendations`, effective worker count, elapsed
  seconds, files per minute, processed files per minute when processing is
  enabled, CPU count, memory note when available, platform, Python version, and
  Pillow version.
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
