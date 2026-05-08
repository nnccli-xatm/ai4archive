"""Command-line interface for phase-one scan QC."""

from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path
import sys

from ._version import __version__
from .acceptance import ACCEPTANCE_JSON, write_acceptance_summary
from .analysis_provider import AnalysisProviderError
from .calibration import CALIBRATION_JSON, write_rules_calibration_summary
from .handoff import write_delivery_handoff_manifest
from .preflight import PreflightConfig, run_preflight, write_preflight_report
from .processing import ProcessingOptions, process_images
from .processing_plan import write_processing_plan
from .processing_review import write_processing_review_package
from .reports import write_reports, write_review_export, write_review_summary
from .rules import RulesProfileError, load_rules_profile
from .scanner import ScanConfig, scan_batch


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--workers must be a positive integer.") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("--workers must be a positive integer.")
    return parsed


def _non_negative_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("threshold must be a non-negative number.") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("threshold must be a non-negative number.")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="archive-scan-qc",
        description="Run phase-one archive scan QC checks and write JSON/CSV reports.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    _add_scan_arguments(parser)
    return parser


def build_preflight_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="archive-scan-qc preflight",
        description="Validate batch configuration before scanning or derivative processing.",
    )
    _add_scan_arguments(parser, include_scan_overrides=False)
    return parser


def _add_scan_arguments(parser: argparse.ArgumentParser, *, include_scan_overrides: bool = True) -> None:
    parser.add_argument("--input", required=True, type=Path, help="Image directory to scan.")
    parser.add_argument("--out", required=True, type=Path, help="Report output directory.")
    parser.add_argument("--project", default="default-project", help="Project identifier.")
    parser.add_argument("--batch", default="default-batch", help="Batch identifier.")
    if include_scan_overrides:
        parser.add_argument(
            "--min-dpi",
            default=None,
            type=int,
            help="Minimum acceptable horizontal and vertical DPI. Defaults to the active rules profile value.",
        )
        parser.add_argument(
            "--name-pattern",
            default=None,
            help="Optional regular expression that each source file stem must match.",
        )
    else:
        parser.set_defaults(min_dpi=None, name_pattern=None)
    parser.add_argument(
        "--manifest-csv",
        default=None,
        type=Path,
        help="Optional batch manifest CSV with a relative_path column.",
    )
    parser.add_argument(
        "--rules-profile",
        default=None,
        type=Path,
        help="Optional JSON rules profile for thresholds, rule enablement, and severity overrides.",
    )
    parser.add_argument(
        "--process-out",
        default=None,
        type=Path,
        help="Optional derivative-image output directory. Originals remain read-only.",
    )
    parser.add_argument(
        "--auto-crop",
        action="store_true",
        help="Conservatively crop page borders in derivative images. Requires --process-out.",
    )
    parser.add_argument(
        "--deskew",
        action="store_true",
        help="Conservatively correct small-angle page skew in derivative images. Requires --process-out.",
    )
    parser.add_argument(
        "--trim-dark-border",
        action="store_true",
        help="Conservatively trim dark scan borders in derivative images. Requires --process-out.",
    )
    parser.add_argument(
        "--despeckle",
        action="store_true",
        help="Replace isolated dark speckles in derivative images. Requires --process-out.",
    )
    parser.add_argument(
        "--resume-processing",
        action="store_true",
        help="Resume derivative processing by skipping existing successful derivatives. Requires --process-out.",
    )
    parser.add_argument(
        "--workers",
        default=None,
        type=_positive_int,
        help="Maximum local worker threads for scan and processing. Use 1 for serial mode.",
    )
    if include_scan_overrides:
        parser.add_argument(
            "--analysis-provider-command",
            default=None,
            help=(
                "Optional local offline analysis provider command. The scanner sends minimized JSONL "
                "records on stdin and reads provider findings as JSONL on stdout."
            ),
        )
    else:
        parser.set_defaults(analysis_provider_command=None)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "benchmark":
        from .benchmark import main as benchmark_main

        return benchmark_main(argv[1:])
    if argv and argv[0] == "run-plan":
        from .run_plan import main as run_plan_main

        return run_plan_main(argv[1:])
    if argv and argv[0] == "preflight":
        return _main_preflight(argv[1:])
    if argv and argv[0] == "review-export":
        return _main_review_export(argv[1:])
    if argv and argv[0] == "review-summary":
        return _main_review_summary(argv[1:])
    if argv and argv[0] == "calibrate-rules":
        return _main_calibrate_rules(argv[1:])
    if argv and argv[0] == "acceptance-summary":
        return _main_acceptance_summary(argv[1:])
    if argv and argv[0] == "delivery-manifest":
        return _main_delivery_manifest(argv[1:])
    if argv and argv[0] == "processing-review-package":
        return _main_processing_review_package(argv[1:])
    if argv and argv[0] == "processing-plan":
        return _main_processing_plan(argv[1:])
    parser = build_parser()
    args = parser.parse_args(argv)
    _validate_processing_flags(parser, args)
    rules_profile = _load_rules_profile(parser, args)

    config = ScanConfig(
        project_id=args.project,
        batch_id=args.batch,
        input_dir=args.input,
        output_dir=args.out,
        min_dpi=args.min_dpi if args.min_dpi is not None else 200,
        name_pattern=args.name_pattern,
        manifest_csv=args.manifest_csv,
        rules_profile=rules_profile,
        workers=args.workers,
        analysis_provider_command=args.analysis_provider_command,
    )
    try:
        report = scan_batch(config)
    except AnalysisProviderError as exc:
        parser.error(str(exc))
    paths = write_reports(report, args.out)

    processing_manifest = (
        process_images(
            report,
            args.input,
            args.process_out,
            ProcessingOptions(
                auto_crop=args.auto_crop,
                deskew=args.deskew,
                trim_dark_border=args.trim_dark_border,
                despeckle=args.despeckle,
                resume_processing=args.resume_processing,
                workers=args.workers,
            ),
        )
        if args.process_out
        else None
    )

    print(f"Scanned {report['summary']['total_files']} files.")
    print(f"Openable: {report['summary']['openable_files']}")
    print(f"Findings: {report['summary']['total_findings']}")
    scan_performance = report["summary"]["performance"]
    print(f"Scan elapsed: {scan_performance['elapsed_seconds']:.3f}s")
    print(f"Scan workers: {scan_performance['effective_workers']} ({scan_performance['mode']})")
    print(f"Scan files/min: {scan_performance['files_per_minute']:.2f}")
    print(f"Scan openable files/min: {scan_performance['openable_files_per_minute']:.2f}")
    if processing_manifest:
        print(f"Processed: {processing_manifest['summary']['processed_files']}")
        print(f"Resume enabled: {processing_manifest['resume']['enabled']}")
        print(f"Resume skipped: {processing_manifest['summary']['skipped_due_to_resume']}")
        print(f"Reprocessed: {processing_manifest['summary']['reprocessed_files']}")
        print(f"Processing failed: {processing_manifest['summary']['failed_files']}")
        audit_records = [record["processing_audit"] for record in processing_manifest["files"] if isinstance(record.get("processing_audit"), dict)]
        warning_files = sum(1 for record in processing_manifest["files"] if record.get("processing_warnings"))
        max_pixel_change = max((record.get("pixel_change_ratio", 0.0) for record in audit_records), default=0.0)
        max_size_change = max((record.get("size_change_ratio", 0.0) for record in audit_records), default=0.0)
        avg_pixel_change = (
            sum(float(record.get("pixel_change_ratio", 0.0)) for record in audit_records) / len(audit_records)
            if audit_records
            else 0.0
        )
        print(f"Processing warnings: {warning_files}")
        print(f"Max pixel change ratio: {max_pixel_change:.6f}")
        print(f"Average pixel change ratio: {avg_pixel_change:.6f}")
        print(f"Max size change ratio: {max_size_change:.6f}")
        processing_performance = processing_manifest["summary"]["performance"]
        print(f"Processing elapsed: {processing_performance['elapsed_seconds']:.3f}s")
        print(f"Processing workers: {processing_performance['effective_workers']} ({processing_performance['mode']})")
        print(f"Processing files/min: {processing_performance['processed_files_per_minute']:.2f}")
        print(f"Processing total files/min: {processing_performance['total_files_per_minute']:.2f}")
        print(f"Processing manifest: {args.process_out / 'processing_manifest.json'}")
        print(f"Processing retry manifest: {args.process_out / 'processing_retry_manifest.json'}")
        print(f"Processing audit summary: {args.process_out / 'processing_audit_summary.json'}")
    for label, path in paths.items():
        print(f"{label}: {path}")
    return 1 if report["summary"]["p0_findings"] else 0


def _main_preflight(argv: list[str]) -> int:
    parser = build_preflight_parser()
    args = parser.parse_args(argv)
    rules_profile, rules_profile_error = _load_rules_profile_for_preflight(args)
    report = run_preflight(
        PreflightConfig(
            project_id=args.project,
            batch_id=args.batch,
            input_dir=args.input,
            output_dir=args.out,
            process_out=args.process_out,
            manifest_csv=args.manifest_csv,
            rules_profile=rules_profile,
            rules_profile_error=rules_profile_error,
            rules_profile_provided=args.rules_profile is not None,
            workers=args.workers,
            auto_crop=args.auto_crop,
            deskew=args.deskew,
            trim_dark_border=args.trim_dark_border,
            despeckle=args.despeckle,
            resume_processing=args.resume_processing,
        )
    )
    path = write_preflight_report(report, args.out)
    print(f"Preflight status: {report['status']}")
    print(f"Candidate files: {report['input_summary']['candidate_file_count']}")
    print(f"Manifest missing: {report['manifest']['missing_count']}")
    print(f"Manifest unexpected: {report['manifest']['unexpected_count']}")
    print(f"Errors: {len(report['errors'])}")
    print(f"Warnings: {len(report['warnings'])}")
    print(f"Preflight report: {path}")
    return 0 if report["status"] == "pass" else 1


def _main_review_export(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="archive-scan-qc review-export",
        description="Export a sensitive local reviewer template from scan_qc_report.json.",
    )
    parser.add_argument("--report", required=True, type=Path, help="Path to scan_qc_report.json.")
    parser.add_argument("--out", required=True, type=Path, help="Output review template CSV or JSON path.")
    args = parser.parse_args(argv)
    try:
        path = write_review_export(args.report, args.out)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f"Review template: {path}")
    print("Sensitivity: contains row-level paths and reviewer notes; keep local and do not upload publicly.")
    return 0


def _main_review_summary(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="archive-scan-qc review-summary",
        description="Write an aggregate-only review_summary.json from a filled review template.",
    )
    parser.add_argument("--review", required=True, type=Path, help="Filled review template CSV or JSON path.")
    parser.add_argument("--out", required=True, type=Path, help="Output aggregate review_summary.json path.")
    args = parser.parse_args(argv)
    try:
        path = write_review_summary(args.review, args.out)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f"Review summary: {path}")
    return 0


def _main_calibrate_rules(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="archive-scan-qc calibrate-rules",
        description="Write aggregate-only rule calibration recommendations from local QC evidence.",
    )
    parser.add_argument(
        "--report",
        required=True,
        action="append",
        type=Path,
        help="Path to scan_qc_report.json. Repeat for multiple local reports.",
    )
    parser.add_argument(
        "--review-summary",
        default=[],
        action="append",
        type=Path,
        help="Optional aggregate review_summary.json. Repeat for multiple summaries.",
    )
    parser.add_argument(
        "--review",
        default=[],
        action="append",
        type=Path,
        help="Optional filled local review template CSV or JSON. Sensitive input; only aggregate counts are emitted.",
    )
    parser.add_argument(
        "--out",
        required=True,
        type=Path,
        help=f"Output JSON path or directory for {CALIBRATION_JSON}.",
    )
    parser.add_argument(
        "--write-suggested-profile",
        default=None,
        type=Path,
        help="Optional draft suggested rules profile JSON path. Never overwrites the source profile.",
    )
    args = parser.parse_args(argv)
    output_path = args.out / CALIBRATION_JSON if args.out.suffix == "" else args.out
    try:
        summary_path, suggested_path = write_rules_calibration_summary(
            args.report,
            output_path,
            review_summary_paths=args.review_summary,
            review_paths=args.review,
            suggested_profile_path=args.write_suggested_profile,
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f"Rules calibration summary: {summary_path}")
    if suggested_path:
        print(f"Suggested draft profile: {suggested_path}")
    print("Sensitivity: aggregate-only summary; source scan_qc_report.json and review templates remain sensitive.")
    return 0


def _main_acceptance_summary(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="archive-scan-qc acceptance-summary",
        description="Write an aggregate-only production acceptance gate summary.",
    )
    parser.add_argument("--run-plan-summary", default=None, type=Path, help="Optional aggregate run_plan_summary.json.")
    parser.add_argument("--review-summary", default=None, type=Path, help="Optional aggregate review_summary.json.")
    parser.add_argument(
        "--processing-audit-summary",
        default=None,
        type=Path,
        help="Optional aggregate processing_audit_summary.json.",
    )
    parser.add_argument("--benchmark-results", default=None, type=Path, help="Optional aggregate benchmark_results.json.")
    parser.add_argument(
        "--min-scan-files-per-minute",
        default=None,
        type=_non_negative_float,
        help="Optional minimum acceptable scan throughput.",
    )
    parser.add_argument(
        "--min-processing-files-per-minute",
        default=None,
        type=_non_negative_float,
        help="Optional minimum acceptable derivative processing throughput.",
    )
    parser.add_argument("--out", required=True, type=Path, help=f"Output JSON path or directory for {ACCEPTANCE_JSON}.")
    args = parser.parse_args(argv)
    output_path = args.out / ACCEPTANCE_JSON if args.out.suffix == "" else args.out
    try:
        path, payload = write_acceptance_summary(
            output_path=output_path,
            run_plan_summary_path=args.run_plan_summary,
            review_summary_path=args.review_summary,
            processing_audit_summary_path=args.processing_audit_summary,
            benchmark_results_path=args.benchmark_results,
            min_scan_files_per_minute=args.min_scan_files_per_minute,
            min_processing_files_per_minute=args.min_processing_files_per_minute,
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f"Acceptance summary: {path}")
    print(f"Acceptance status: {payload['status']}")
    print("Sensitivity: aggregate-only summary; no filenames, paths, hashes, thumbnails, row-level findings, notes, OCR, or image content.")
    return 0 if payload["pass"] else 1


def _main_processing_review_package(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="archive-scan-qc processing-review-package",
        description="Write a sensitive local JSON and standalone HTML review package from processing_manifest.json.",
    )
    parser.add_argument("--manifest", required=True, type=Path, help="Path to processing_manifest.json.")
    parser.add_argument("--out", required=True, type=Path, help="Output directory for local review package artifacts.")
    args = parser.parse_args(argv)
    try:
        json_path, html_path = write_processing_review_package(args.manifest, args.out)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f"Processing review package JSON: {json_path}")
    print(f"Processing review package HTML: {html_path}")
    print("Sensitivity: local-only row-level evidence; do not use as public aggregate evidence.")
    return 0


def _main_processing_plan(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="archive-scan-qc processing-plan",
        description="Write a sensitive local dry-run processing plan from scan_qc_report.json without derivative images.",
    )
    parser.add_argument("--report", required=True, type=Path, help="Path to scan_qc_report.json.")
    parser.add_argument("--input", required=True, type=Path, help="Image directory used by the scan report.")
    parser.add_argument("--out", required=True, type=Path, help="Output directory for processing_plan.json and processing_plan.csv.")
    parser.add_argument("--auto-crop", action="store_true", help="Plan conservative page-border crop candidates.")
    parser.add_argument("--deskew", action="store_true", help="Plan conservative small-angle deskew candidates.")
    parser.add_argument("--trim-dark-border", action="store_true", help="Plan conservative dark scan border trim candidates.")
    parser.add_argument("--despeckle", action="store_true", help="Plan isolated dark speckle cleanup candidates.")
    args = parser.parse_args(argv)
    try:
        json_path, csv_path, plan = write_processing_plan(
            args.report,
            args.input,
            args.out,
            ProcessingOptions(
                auto_crop=args.auto_crop,
                deskew=args.deskew,
                trim_dark_border=args.trim_dark_border,
                despeckle=args.despeckle,
            ),
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    summary = plan["summary"]
    print(f"Processing plan JSON: {json_path}")
    print(f"Processing plan CSV: {csv_path}")
    print(f"Planned files: {summary['planned_files']}")
    print(f"Skipped/unopenable files: {summary['skipped_files'] + summary['unopenable_files']}")
    print("Derivative images written: no")
    print("Sensitivity: sensitive local row-level evidence; contains paths/hashes, no thumbnails or image bytes.")
    return 0


def _main_delivery_manifest(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="archive-scan-qc delivery-manifest",
        description="Write deterministic local JSON and CSV handoff manifests for selected delivery evidence.",
    )
    parser.add_argument("--scan-report", default=None, type=Path, help="Optional scan_qc_report.json or related scan report.")
    parser.add_argument(
        "--processing-audit-summary",
        default=None,
        type=Path,
        help="Optional aggregate processing_audit_summary.json.",
    )
    parser.add_argument("--acceptance-summary", default=None, type=Path, help="Optional aggregate acceptance_summary.json.")
    parser.add_argument("--review-summary", default=None, type=Path, help="Optional aggregate review_summary.json.")
    parser.add_argument("--benchmark-results", default=None, type=Path, help="Optional aggregate benchmark_results.json or CSV.")
    parser.add_argument("--processing-manifest", default=None, type=Path, help="Optional sensitive local processing_manifest.json.")
    parser.add_argument(
        "--artifact",
        default=[],
        action="append",
        type=Path,
        help="Additional local evidence file. Repeat as needed; unknown artifacts are marked sensitive.",
    )
    parser.add_argument("--out", required=True, type=Path, help="Output directory for delivery handoff JSON and CSV manifests.")
    args = parser.parse_args(argv)

    artifacts = _delivery_manifest_artifacts(args)
    try:
        json_path, csv_path, payload = write_delivery_handoff_manifest(artifacts, args.out)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f"Delivery handoff manifest JSON: {json_path}")
    print(f"Delivery handoff manifest CSV: {csv_path}")
    print(f"Artifacts: {payload['summary']['artifact_count']}")
    print(f"Aggregate/public-safe: {payload['summary']['aggregate_public_safe_count']}")
    print(f"Sensitive local evidence: {payload['summary']['sensitive_local_evidence_count']}")
    print("Source images copied: no")
    print("Uploads performed: no")
    return 0


def _delivery_manifest_artifacts(args: argparse.Namespace) -> list[tuple[str, Path]]:
    artifacts: list[tuple[str, Path]] = []
    for role in [
        "scan_report",
        "processing_audit_summary",
        "acceptance_summary",
        "review_summary",
        "benchmark_results",
        "processing_manifest",
    ]:
        path = getattr(args, role)
        if path is not None:
            artifacts.append((role, path))
    artifacts.extend(("artifact", path) for path in args.artifact)
    return artifacts


def _validate_processing_flags(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.auto_crop and not args.process_out:
        parser.error("--auto-crop requires --process-out")
    if args.deskew and not args.process_out:
        parser.error("--deskew requires --process-out")
    if args.trim_dark_border and not args.process_out:
        parser.error("--trim-dark-border requires --process-out")
    if args.despeckle and not args.process_out:
        parser.error("--despeckle requires --process-out")
    if args.resume_processing and not args.process_out:
        parser.error("--resume-processing requires --process-out")


def _load_rules_profile(parser: argparse.ArgumentParser, args: argparse.Namespace):
    try:
        rules_profile = load_rules_profile(args.rules_profile) if args.rules_profile else None
    except RulesProfileError as exc:
        parser.error(str(exc))
    if rules_profile and args.min_dpi is not None:
        rules_profile = replace(rules_profile, min_dpi=args.min_dpi)
    if rules_profile and args.name_pattern is not None:
        rules_profile = replace(rules_profile, name_pattern=args.name_pattern)
    return rules_profile


def _load_rules_profile_for_preflight(args: argparse.Namespace):
    if not args.rules_profile:
        return None, None
    try:
        return load_rules_profile(args.rules_profile), None
    except RulesProfileError:
        return None, "Rules profile could not be loaded or validated."


if __name__ == "__main__":
    raise SystemExit(main())
