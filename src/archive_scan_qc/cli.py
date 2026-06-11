"""Command-line interface for phase-one scan QC."""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys

from ._version import __version__
from .acceptance import ACCEPTANCE_JSON, write_acceptance_summary
from .analysis_provider import AnalysisProviderError
from .artifact_readiness import ARTIFACT_READINESS_JSON, write_artifact_readiness_checklist
from .batch_rename import (
    BATCH_RENAME_APPLY_JSON,
    BATCH_RENAME_PLAN_JSON,
    apply_batch_rename_plan,
    write_batch_rename_plan,
)
from .calibration import CALIBRATION_JSON, write_rules_calibration_summary
from .case_split import CASE_SPLIT_APPLY_JSON, CASE_SPLIT_PLAN_JSON, apply_case_split_plan, write_case_split_plan
from .capability_probe import CapabilityProbeConfig, run_capability_probe, write_capability_probe
from .deep_inspection_provider import (
    DeepInspectionProviderConfigError,
    build_deep_inspection_provider_probe,
    load_deep_inspection_provider_config,
    write_deep_inspection_provider_probe,
)
from .deep_inspection_candidates import (
    build_deep_inspection_candidate_summary,
    load_deep_inspection_candidate_inputs,
    write_deep_inspection_candidate_summary,
)
from .evidence_bundle import EVIDENCE_BUNDLE_JSON, write_evidence_bundle_summary
from .final_handoff import FINAL_HANDOFF_JSON, write_final_handoff_summary
from .handoff import write_delivery_handoff_manifest
from .image_processing_capability_smoke import run_image_processing_capability_smoke
from .ocr_validation import (
    OCR_PREPROCESSING_OCR_VALIDATION_JSON,
    OCR_PROVIDER_PROBE_JSON,
    OcrProviderProbeConfig,
    OcrValidationConfig,
    write_ocr_preprocessing_ocr_validation,
    write_ocr_provider_probe,
)
from .preflight import PreflightConfig, run_preflight, write_preflight_report
from .production_runner import PRODUCTION_RUN_PROGRESS_JSON, PRODUCTION_RUN_SUMMARY_JSON, ProductionRunConfig, run_production_folder
from .processing import ProcessingOptions, process_images
from .processing_plan import write_processing_plan
from .processing_review import write_processing_review_package
from .production_review_queue import PRODUCTION_REVIEW_QUEUE_JSON, write_production_review_queue
from .production_rehearsal import ProductionRehearsalConfig, run_production_rehearsal
from .private_validation import PRIVATE_VALIDATION_AGGREGATE_JSON, write_private_validation_aggregate
from .public_capability_contract import build_public_capability_contract, write_public_capability_contract
from .reports import write_reports, write_review_export, write_review_summary
from .review_decisions import write_review_decision_verification_summary
from .rework import write_rework_action_list
from .rule_templates import (
    build_rule_template_catalog,
    build_rule_template_dry_run,
    load_scan_report,
    write_rule_template_catalog,
    write_rule_template_dry_run,
)
from .rules import (
    RULE_TEMPLATE_IDS,
    RulesProfileError,
    load_rules_profile_selection,
    processing_defaults_for_rule_template,
    processing_profile_for_rule_template,
)
from .sampling import DEFAULT_SAMPLE_RATIO, write_acceptance_sampling_export
from .scanner import ScanConfig, scan_batch
from .validation_index import VALIDATION_INDEX_JSON, write_public_safe_validation_index
from .workbench_summary import WORKBENCH_SUMMARY_JSON, write_workbench_public_summary


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
        "--rule-template",
        default=None,
        choices=RULE_TEMPLATE_IDS,
        help=(
            "Optional processing rules template. Built-ins include archival-safe-v1, "
            "text-clean-readable-v1, print-clean-v1, ocr-preprocess-leptonica-v1, "
            "ocr-preprocess-opencv-local-v1, ocr-preprocess-v1, "
            "photo-mixed-safe-v1, and legacy IDs. "
            "Use custom with --rules-profile."
        ),
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
        "--scanner-gutter-trim",
        action="store_true",
        help="Conservatively trim narrow uniform light-gray scanner gutters. Requires --process-out.",
    )
    parser.add_argument(
        "--despeckle",
        action="store_true",
        help="Replace isolated dark speckles in derivative images. Requires --process-out.",
    )
    parser.add_argument(
        "--normalize-tones",
        action="store_true",
        help="Conservatively normalize gray/dark low-contrast neutral pages. Requires --process-out.",
    )
    parser.add_argument(
        "--normalize-paper-color-cast",
        action="store_true",
        help="Conservatively neutralize mild uniform scanner color cast on otherwise plain bright paper. Requires --process-out.",
    )
    parser.add_argument(
        "--lighten-edge-shadow",
        action="store_true",
        help="Conservatively lighten narrow neutral page-edge shadows. Requires --process-out.",
    )
    parser.add_argument(
        "--lighten-corner-shadows",
        action="store_true",
        help="Conservatively lighten smooth neutral corner shadows. Requires --process-out.",
    )
    parser.add_argument(
        "--lighten-background-stains",
        action="store_true",
        help="Conservatively lighten small neutral stains on light page backgrounds. Requires --process-out.",
    )
    parser.add_argument(
        "--lighten-fold-shadows",
        action="store_true",
        help="Conservatively lighten narrow fold shadows in clean page backgrounds. Requires --process-out.",
    )
    parser.add_argument(
        "--level-illumination-gradient",
        action="store_true",
        help="Conservatively level mild smooth scanner illumination gradients on bright paper. Requires --process-out.",
    )
    parser.add_argument(
        "--clean-bleed-through",
        action="store_true",
        help="Conservatively clean faint reverse-side ghosts in open light backgrounds. Requires --process-out.",
    )
    parser.add_argument(
        "--lighten-scanlines",
        action="store_true",
        help="Conservatively lighten low-contrast scanlines on protected light backgrounds. Requires --process-out.",
    )
    parser.add_argument(
        "--enhance-faded-text",
        action="store_true",
        help="Conservatively darken stable low-contrast neutral text on light paper. Requires --process-out.",
    )
    parser.add_argument(
        "--sharpen-text-edges",
        action="store_true",
        help="Conservatively sharpen stable neutral blurred text edges on light paper. Requires --process-out.",
    )
    parser.add_argument(
        "--despeckle-backend",
        choices=("fallback", "numpy"),
        default="fallback",
        help="Despeckle processing backend. Defaults to conservative fallback; numpy is opt-in.",
    )
    parser.add_argument(
        "--resume-processing",
        action="store_true",
        help="Resume derivative processing by skipping existing successful derivatives. Requires --process-out.",
    )
    parser.add_argument(
        "--reuse-scan-measurements",
        action="store_true",
        help="Reuse complete scan-stage measurements during derivative processing when safe. Requires --process-out.",
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
    if argv and argv[0] == "production-run":
        return _main_production_run(argv[1:])
    if argv and argv[0] == "production-rehearsal":
        return _main_production_rehearsal(argv[1:])
    if argv and argv[0] == "production-workbench":
        from .local_workbench import main as local_workbench_main

        return local_workbench_main(argv[1:])
    if argv and argv[0] == "capability-probe":
        return _main_capability_probe(argv[1:])
    if argv and argv[0] == "image-processing-capability-smoke":
        return _main_image_processing_capability_smoke(argv[1:])
    if argv and argv[0] == "public-capability-contract":
        return _main_public_capability_contract(argv[1:])
    if argv and argv[0] == "service-api":
        return _main_service_api(argv[1:])
    if argv and argv[0] == "rule-template-catalog":
        return _main_rule_template_catalog(argv[1:])
    if argv and argv[0] == "rule-template-dry-run":
        return _main_rule_template_dry_run(argv[1:])
    if argv and argv[0] == "deep-inspection-provider-probe":
        return _main_deep_inspection_provider_probe(argv[1:])
    if argv and argv[0] == "deep-inspection-candidate-summary":
        return _main_deep_inspection_candidate_summary(argv[1:])
    if argv and argv[0] == "review-export":
        return _main_review_export(argv[1:])
    if argv and argv[0] == "review-summary":
        return _main_review_summary(argv[1:])
    if argv and argv[0] == "review-decisions-verify":
        return _main_review_decisions_verify(argv[1:])
    if argv and argv[0] == "calibrate-rules":
        return _main_calibrate_rules(argv[1:])
    if argv and argv[0] == "acceptance-summary":
        return _main_acceptance_summary(argv[1:])
    if argv and argv[0] == "acceptance-sampling-export":
        return _main_acceptance_sampling_export(argv[1:])
    if argv and argv[0] == "delivery-manifest":
        return _main_delivery_manifest(argv[1:])
    if argv and argv[0] == "evidence-bundle-verify":
        return _main_evidence_bundle_verify(argv[1:])
    if argv and argv[0] == "final-handoff-summary":
        return _main_final_handoff_summary(argv[1:])
    if argv and argv[0] == "public-safe-validation-index":
        return _main_public_safe_validation_index(argv[1:])
    if argv and argv[0] == "private-validation-aggregate":
        return _main_private_validation_aggregate(argv[1:])
    if argv and argv[0] == "ocr-provider-probe":
        return _main_ocr_provider_probe(argv[1:])
    if argv and argv[0] == "ocr-preprocessing-ocr-validation":
        return _main_ocr_preprocessing_ocr_validation(argv[1:])
    if argv and argv[0] == "workbench-summary":
        return _main_workbench_summary(argv[1:])
    if argv and argv[0] == "artifact-readiness-checklist":
        return _main_artifact_readiness_checklist(argv[1:])
    if argv and argv[0] == "processing-review-package":
        return _main_processing_review_package(argv[1:])
    if argv and argv[0] == "processing-plan":
        return _main_processing_plan(argv[1:])
    if argv and argv[0] == "rework-action-list":
        return _main_rework_action_list(argv[1:])
    if argv and argv[0] == "production-review-queue":
        return _main_production_review_queue(argv[1:])
    if argv and argv[0] == "batch-rename-plan":
        return _main_batch_rename_plan(argv[1:])
    if argv and argv[0] == "batch-rename-apply":
        return _main_batch_rename_apply(argv[1:])
    if argv and argv[0] == "case-split-plan":
        return _main_case_split_plan(argv[1:])
    if argv and argv[0] == "case-split-apply":
        return _main_case_split_apply(argv[1:])
    parser = build_parser()
    args = parser.parse_args(argv)
    _validate_processing_flags(parser, args)
    rules_profile = _load_rules_profile(parser, args)
    if args.process_out:
        _apply_rule_template_processing_defaults(args)

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
                scanner_gutter_trim=args.scanner_gutter_trim,
                despeckle=args.despeckle,
                normalize_tones=args.normalize_tones,
                normalize_paper_color_cast=args.normalize_paper_color_cast,
                lighten_edge_shadow=args.lighten_edge_shadow,
                lighten_corner_shadows=args.lighten_corner_shadows,
                lighten_background_stains=args.lighten_background_stains,
                lighten_fold_shadows=args.lighten_fold_shadows,
                level_illumination_gradient=args.level_illumination_gradient,
                clean_bleed_through=args.clean_bleed_through,
                lighten_scanlines=args.lighten_scanlines,
                enhance_faded_text=args.enhance_faded_text,
                sharpen_text_edges=args.sharpen_text_edges,
                ocr_preprocess=getattr(args, "ocr_preprocess", False),
                ocr_binary=getattr(args, "ocr_binary", False),
                despeckle_content_type_check=getattr(args, "despeckle_content_type_check", True),
                despeckle_backend=args.despeckle_backend,
                resume_processing=args.resume_processing,
                reuse_scan_measurements=args.reuse_scan_measurements,
                processing_profile=_processing_profile_for_args(args),
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
            rules_profile_provided=args.rules_profile is not None or args.rule_template is not None,
            workers=args.workers,
            auto_crop=args.auto_crop,
            deskew=args.deskew,
            trim_dark_border=args.trim_dark_border,
            scanner_gutter_trim=args.scanner_gutter_trim,
            despeckle=args.despeckle,
            normalize_tones=args.normalize_tones,
            normalize_paper_color_cast=args.normalize_paper_color_cast,
            resume_processing=args.resume_processing,
            lighten_edge_shadow=args.lighten_edge_shadow,
            lighten_corner_shadows=args.lighten_corner_shadows,
            lighten_background_stains=args.lighten_background_stains,
            lighten_fold_shadows=args.lighten_fold_shadows,
            level_illumination_gradient=args.level_illumination_gradient,
            clean_bleed_through=args.clean_bleed_through,
            lighten_scanlines=args.lighten_scanlines,
            enhance_faded_text=args.enhance_faded_text,
            sharpen_text_edges=args.sharpen_text_edges,
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


def _main_production_run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="archive-scan-qc production-run",
        description="运行本地扫描图片批量质检和处理后图片生成流程，并写出生产界面可读取的状态文件。",
    )
    parser.add_argument("--input", required=True, type=Path, help="要处理的扫描图片文件夹。")
    parser.add_argument("--derivatives-out", required=True, type=Path, help="处理后图片输出文件夹。")
    parser.add_argument(
        "--metadata-out",
        required=True,
        type=Path,
        help="生产摘要、进度和后台报告输出文件夹。",
    )
    parser.add_argument("--project", default="default-project", help="项目编号。")
    parser.add_argument("--batch", default="default-batch", help="批次编号。")
    parser.add_argument("--min-dpi", default=None, type=int, help="最低 DPI 要求。")
    parser.add_argument("--dpi-purpose", default=None, choices=("standard", "com", "reproduction", "print"), help="按用途分级的最低分辨率：standard=200dpi, com=300dpi, reproduction=600dpi, print=300dpi。")
    parser.add_argument("--name-pattern", default=None, help="可选文件名规则。")
    parser.add_argument("--manifest-csv", default=None, type=Path, help="可选批次清单 CSV，需包含 relative_path 列。")
    parser.add_argument("--rules-profile", default=None, type=Path, help="可选质检规则配置 JSON。")
    parser.add_argument(
        "--rule-template",
        default=None,
        choices=RULE_TEMPLATE_IDS,
        help=(
            "图像处理规则模板：archival-safe-v1、text-clean-readable-v1、print-clean-v1、ocr-preprocess-leptonica-v1、"
            "ocr-preprocess-opencv-local-v1、ocr-preprocess-v1、"
            "photo-mixed-safe-v1、legacy IDs 或 custom。"
        ),
    )
    parser.add_argument("--auto-crop", action="store_true", help="保守裁切处理后图片边缘。")
    parser.add_argument("--deskew", action="store_true", help="保守校正处理后图片的小角度倾斜。")
    parser.add_argument("--trim-dark-border", action="store_true", help="保守清理扫描黑边。")
    parser.add_argument("--scanner-gutter-trim", action="store_true", help="保守裁掉窄幅浅灰扫描台边。")
    parser.add_argument("--despeckle", action="store_true", help="清理孤立黑点。")
    parser.add_argument("--normalize-tones", action="store_true", help="保守校正偏灰、偏暗的低对比度页面。")
    parser.add_argument("--normalize-paper-color-cast", action="store_true", help="保守校正浅色纸面上的轻微统一扫描偏色。")
    parser.add_argument("--lighten-edge-shadow", action="store_true", help="保守减淡不接触正文的页边窄幅阴影。")
    parser.add_argument("--lighten-corner-shadows", action="store_true", help="保守减淡不接触正文和标记的页面角落阴影。")
    parser.add_argument("--lighten-background-stains", action="store_true", help="保守减淡浅色纸面上不接触正文和印章的小范围浅斑。")
    parser.add_argument("--lighten-fold-shadows", action="store_true", help="保守减淡干净背景中的窄幅折痕阴影。")
    parser.add_argument("--level-illumination-gradient", action="store_true", help="保守校平浅色纸面上的轻微平滑扫描明暗渐变。")
    parser.add_argument("--clean-bleed-through", action="store_true", help="保守清理浅色背景中不接触正文和印章的轻微背面透印。")
    parser.add_argument("--lighten-scanlines", action="store_true", help="保守减淡浅色背景中不接触正文和档案原貌的低对比扫描线。")
    parser.add_argument("--enhance-faded-text", action="store_true", help="保守增强浅色纸面上的低对比浅墨正文。")
    parser.add_argument("--sharpen-text-edges", action="store_true", help="保守增强浅色纸面上轻微发虚正文的文字边缘清晰度。")
    parser.add_argument(
        "--despeckle-backend",
        choices=("fallback", "numpy"),
        default="fallback",
        help="去黑点处理后端，默认使用保守模式，numpy 需显式开启。",
    )
    parser.add_argument(
        "--resume-processing",
        dest="resume_processing",
        action="store_true",
        default=True,
        help="尽量跳过已经成功生成的处理后图片（生产运行默认开启）。",
    )
    parser.add_argument(
        "--no-resume-processing",
        dest="resume_processing",
        action="store_false",
        help="诊断时强制重新生成处理后图片。",
    )
    parser.add_argument(
        "--reuse-scan-measurements",
        action="store_true",
        help="在安全时复用质检阶段已完成的测量结果，加快处理后图片生成。",
    )
    parser.add_argument("--workers", default=None, type=_positive_int, help="本机最大工作线程数，填 1 表示单线程。")
    parser.add_argument(
        "--analysis-provider-command",
        default=None,
        help="可选本地离线深度分析命令。",
    )
    args = parser.parse_args(argv)
    rules_profile = _load_rules_profile(parser, args)
    _apply_rule_template_processing_defaults(args)
    try:
        summary = run_production_folder(
            ProductionRunConfig(
                project_id=args.project,
                batch_id=args.batch,
                input_dir=args.input,
                derivative_output_dir=args.derivatives_out,
                metadata_output_dir=args.metadata_out,
                min_dpi=args.min_dpi if args.min_dpi is not None else 200,
                name_pattern=args.name_pattern,
                manifest_csv=args.manifest_csv,
                rules_profile=rules_profile,
                auto_crop=args.auto_crop,
                deskew=args.deskew,
                trim_dark_border=args.trim_dark_border,
                scanner_gutter_trim=args.scanner_gutter_trim,
                despeckle=args.despeckle,
                normalize_tones=args.normalize_tones,
                normalize_paper_color_cast=args.normalize_paper_color_cast,
                lighten_edge_shadow=args.lighten_edge_shadow,
                lighten_corner_shadows=args.lighten_corner_shadows,
                lighten_background_stains=args.lighten_background_stains,
                lighten_fold_shadows=args.lighten_fold_shadows,
                level_illumination_gradient=args.level_illumination_gradient,
                clean_bleed_through=args.clean_bleed_through,
                lighten_scanlines=args.lighten_scanlines,
                enhance_faded_text=args.enhance_faded_text,
                sharpen_text_edges=args.sharpen_text_edges,
                ocr_preprocess=getattr(args, "ocr_preprocess", False),
                ocr_binary=getattr(args, "ocr_binary", False),
                despeckle_content_type_check=getattr(args, "despeckle_content_type_check", True),
                despeckle_backend=args.despeckle_backend,
                resume_processing=args.resume_processing,
                reuse_scan_measurements=args.reuse_scan_measurements,
                workers=args.workers,
                analysis_provider_command=args.analysis_provider_command,
                processing_profile=_processing_profile_for_args(args),
            )
        )
    except KeyboardInterrupt:
        parser.exit(1, "archive-scan-qc production-run: interrupted\n")
    except Exception as exc:
        parser.exit(1, f"archive-scan-qc production-run: error: {exc}\n")
    print(f"生产状态: {summary['status_label_zh']} ({summary['status']})")
    print(f"操作提示: {summary['operator_summary']['message_zh']}")
    print(f"扫描原图数量: {summary['operator_summary']['total_source_images']}")
    print(f"已生成处理后图片: {summary['operator_summary']['derivative_images_ready']}")
    print(f"需要人工处理: {summary['operator_summary']['files_needing_attention']}")
    print(f"处理后图片文件夹: {summary['operator_summary']['derivative_image_folder']}")
    print(f"生产摘要: {args.metadata_out / PRODUCTION_RUN_SUMMARY_JSON}")
    print(f"处理进度: {args.metadata_out / PRODUCTION_RUN_PROGRESS_JSON}")
    print("原图是否被修改: 否")
    return 0 if summary["ready_for_operator_handoff"] else 1


def _main_production_rehearsal(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="archive-scan-qc production-rehearsal",
        description="生成合成图片并一键演练本机生产处理和复核产物。",
    )
    parser.add_argument(
        "--root",
        default=None,
        type=Path,
        help="可选演练根文件夹。默认创建一个临时文件夹。",
    )
    parser.add_argument("--project", default="local-rehearsal", help="项目编号。")
    parser.add_argument("--batch", default="synthetic-batch", help="批次编号。")
    parser.add_argument("--workers", default=1, type=_positive_int, help="本机最大工作线程数，填 1 表示单线程。")
    parser.add_argument("--keep-existing", action="store_true", help="允许使用已有内容的演练根文件夹。")
    parser.add_argument("--launch-workbench", action="store_true", help="生成演练后立即打开已预填文件夹的本地生产工作台。")
    parser.add_argument("--host", default="127.0.0.1", help="启动工作台时使用的本机地址，只允许回环地址。")
    parser.add_argument("--port", default=8765, type=int, help="启动工作台时使用的本机端口。")
    parser.add_argument("--no-open", action="store_true", help="启动工作台时不自动打开浏览器。")
    args = parser.parse_args(argv)
    try:
        rehearsal = run_production_rehearsal(
            ProductionRehearsalConfig(
                root_dir=args.root,
                project_id=args.project,
                batch_id=args.batch,
                workers=args.workers,
                keep_existing=args.keep_existing,
            )
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print("本机生产演练已生成。")
    print(f"演练文件夹: {rehearsal['root_dir']}")
    print(f"扫描原图文件夹: {rehearsal['input_dir']}")
    print(f"处理后输出文件夹: {rehearsal['derivatives_dir']}")
    print(f"本机状态文件夹: {rehearsal['metadata_dir']}")
    print(f"生产状态: {rehearsal['status_label_zh']}")
    print(f"操作提示: {rehearsal['operator_message_zh']}")
    print(f"合成原图数量: {rehearsal['source_count']}")
    print(f"已生成处理后图片: {rehearsal['derivative_count']}")
    print(f"待人工确认条目: {rehearsal['review_queue_items']}")
    print("下一步:")
    print("1. 运行: archive-scan-qc production-rehearsal --launch-workbench")
    print("2. 工作台会自动带入演练文件夹；也可以继续手动填写文件夹。")
    print("3. 查看合成图片并练习开始、复核、完成并导出。")
    print("说明: 本演练只使用合成图片，不需要私有图片，也不会调用外部服务。")
    if args.launch_workbench:
        from .local_workbench import main as local_workbench_main

        print("正在启动本地生产工作台。")
        return local_workbench_main(
            [
                "--host",
                args.host,
                "--port",
                str(args.port),
                "--input-dir",
                str(rehearsal["input_dir"]),
                "--derivatives-dir",
                str(rehearsal["derivatives_dir"]),
                "--metadata-dir",
                str(rehearsal["metadata_dir"]),
                *(["--no-open"] if args.no_open else []),
            ]
        )
    return 0


def _main_capability_probe(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="archive-scan-qc capability-probe",
        description="Report aggregate optional local GPU/model provider readiness without running inference.",
    )
    parser.add_argument(
        "--analysis-provider-command",
        default=None,
        help="Optional local provider command to count as configured. The command is not executed.",
    )
    parser.add_argument(
        "--gpu-acceleration-enabled",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Declare whether GPU acceleration is configured. Defaults to safe environment flag detection.",
    )
    parser.add_argument(
        "--model-acceleration-enabled",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Declare whether model acceleration is configured. Defaults to safe environment flag detection.",
    )
    parser.add_argument(
        "--no-torch-cuda-check",
        action="store_true",
        help="Skip importing torch for CUDA visibility even when torch is installed.",
    )
    parser.add_argument("--out", default=None, type=Path, help="Optional JSON output path or directory.")
    args = parser.parse_args(argv)

    report = run_capability_probe(
        CapabilityProbeConfig(
            analysis_provider_command=args.analysis_provider_command,
            gpu_acceleration_enabled=args.gpu_acceleration_enabled,
            model_acceleration_enabled=args.model_acceleration_enabled,
            include_torch_cuda=not args.no_torch_cuda_check,
        )
    )
    if args.out:
        path = write_capability_probe(report, args.out)
        print(f"Capability probe report: {path}")
    print(f"Capability probe status: {report['status']}")
    print(f"Optional provider packages found: {len(report['readiness']['provider_packages_found'])}")
    print(f"GPU visible count: {report['gpu_provider_visibility']['gpu_visible_count']}")
    print(f"GPU acceleration configured: {report['readiness']['gpu_acceleration_configured']}")
    print(f"Model acceleration configured: {report['readiness']['model_acceleration_configured']}")
    print("Inference run: no")
    print("Privacy: aggregate-only; no image paths, filenames, hashes, OCR text, thumbnails, secrets, or row-level findings.")
    return 0


def _main_public_capability_contract(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="archive-scan-qc public-capability-contract",
        description="Write the public-safe stable CLI, schema, and backend capability contract.",
    )
    parser.add_argument("--out", default=None, type=Path, help="Optional JSON output path or directory.")
    args = parser.parse_args(argv)

    report = build_public_capability_contract()
    if args.out:
        path = write_public_capability_contract(report, args.out)
        print(f"Public capability contract: {path}")
    public_safe_artifacts = sum(
        1
        for artifact in report["output_artifacts"]
        if artifact.get("stability") == "stable_public_safe_aggregate"
    )
    print(f"Contract status: {report['status']}")
    print(f"Schema version: {report['schema_version']}")
    print(f"Stable CLI commands: {len(report['public_cli']['stable_commands'])}")
    print(f"Tracked artifacts: {len(report['output_artifacts'])}")
    print(f"Public-safe aggregate artifacts: {public_safe_artifacts}")
    print("Image processing run: no")
    print("Provider commands run: no")
    print("Privacy: public-safe aggregate contract; no image paths, filenames, hashes, OCR text, thumbnails, or image content.")
    return 0


def _main_service_api(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="archive-scan-qc service-api",
        description="Run the prototype local HTTP service API.",
    )
    parser.add_argument("--service-root", required=True, type=Path, help="Root directory for isolated service jobs.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host. Defaults to 127.0.0.1.")
    parser.add_argument("--port", default=8765, type=int, help="Bind port. Defaults to 8765.")
    args = parser.parse_args(argv)

    from .service_http import serve_service_api

    print(f"Service API listening on http://{args.host}:{args.port}")
    print("Local-only prototype; JSON responses are public-safe aggregates.")
    try:
        serve_service_api(service_root=args.service_root, host=args.host, port=args.port)
    except KeyboardInterrupt:
        return 0
    return 0


def _main_image_processing_capability_smoke(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="archive-scan-qc image-processing-capability-smoke",
        description="Run a public-safe synthetic scan and derivative-processing smoke check.",
    )
    parser.add_argument("--out", default=None, type=Path, help="Optional JSON output path or directory.")
    parser.add_argument(
        "--despeckle-backend",
        choices=("fallback", "numpy"),
        default="fallback",
        help="Synthetic despeckle backend to exercise. Defaults to fallback; numpy is opt-in.",
    )
    parser.add_argument("--workers", default=1, type=_positive_int, help="Local worker count for the synthetic run.")
    args = parser.parse_args(argv)

    try:
        path, report, quality_path = run_image_processing_capability_smoke(
            output_path=args.out,
            despeckle_backend=args.despeckle_backend,
            workers=args.workers,
        )
    except (OSError, ValueError) as exc:
        parser.exit(1, f"archive-scan-qc image-processing-capability-smoke: error: {exc}\n")
    if path:
        print(f"Image processing capability smoke: {path}")
    if quality_path:
        print(f"Processing quality summary: {quality_path}")
    print(f"Smoke status: {report['status']}")
    print(f"Schema version: {report['schema_version']}")
    print(f"Synthetic fixtures: {report['counts']['synthetic_fixture_count']}")
    print(f"Processed files: {report['counts']['processed_files']}")
    print(f"Processing failed files: {report['counts']['failed_files']}")
    print(f"Guardrail failed files: {report['counts']['guardrail_failed_files']}")
    print(f"Quality baseline status: {report['quality_baseline']['status']}")
    print(f"Source images modified: {'yes' if report['processing_run']['source_images_modified'] else 'no'}")
    print("Image processing run: yes")
    print("Private images read: no")
    print("Provider commands run: no")
    print("Privacy: public-safe aggregate smoke; no image paths, filenames, hashes, OCR text, thumbnails, or image content.")
    return 0 if report["status"] == "pass" else 1


def _main_rule_template_catalog(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="archive-scan-qc rule-template-catalog",
        description="Write a public-safe catalog of built-in image-processing rule templates.",
    )
    parser.add_argument("--out", default=None, type=Path, help="Optional JSON output path or directory.")
    args = parser.parse_args(argv)
    payload = build_rule_template_catalog()
    if args.out:
        path = write_rule_template_catalog(payload, args.out)
        print(f"Rule template catalog: {path}")
    print(f"Catalog status: {payload['status']}")
    print(f"Schema version: {payload['schema_version']}")
    print(f"Templates: {len(payload['templates'])}")
    print("Image processing run: no")
    print("Derivative images written: no")
    print("Privacy: public-safe aggregate template catalog; no paths, filenames, hashes, thumbnails, OCR text, or image content.")
    return 0


def _main_rule_template_dry_run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="archive-scan-qc rule-template-dry-run",
        description="Write a public-safe aggregate dry-run plan for a rule template without derivative images.",
    )
    parser.add_argument("--rule-template", required=True, choices=RULE_TEMPLATE_IDS, help="Template ID to dry-run.")
    parser.add_argument("--scan-report", default=None, type=Path, help="Optional sensitive local scan_qc_report.json.")
    parser.add_argument("--out", default=None, type=Path, help="Optional JSON output path or directory.")
    args = parser.parse_args(argv)
    try:
        payload = build_rule_template_dry_run(
            rule_template=args.rule_template,
            scan_report=load_scan_report(args.scan_report),
        )
    except (OSError, ValueError, RulesProfileError) as exc:
        parser.error(str(exc))
    if args.out:
        path = write_rule_template_dry_run(payload, args.out)
        print(f"Rule template dry-run: {path}")
    enabled_operations = [item for item in payload["planned_operations"] if item["enabled"]]
    print(f"Dry-run status: {payload['status']}")
    print(f"Schema version: {payload['schema_version']}")
    print(f"Rule template: {payload['template']['id']}")
    print(f"Enabled operations: {len(enabled_operations)}")
    print(f"Risk codes: {len(payload['risk_codes'])}")
    print("Derivative images written: no")
    print("Privacy: public-safe aggregate template dry-run; no paths, filenames, hashes, thumbnails, OCR text, or image content.")
    return 0


def _main_deep_inspection_provider_probe(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="archive-scan-qc deep-inspection-provider-probe",
        description="Validate optional deep-inspection provider metadata without running inference.",
    )
    parser.add_argument(
        "--provider-config",
        default=None,
        type=Path,
        help="Optional JSON provider metadata config. No provider command is executed.",
    )
    parser.add_argument("--out", default=None, type=Path, help="Optional JSON output path or directory.")
    args = parser.parse_args(argv)

    try:
        config = load_deep_inspection_provider_config(args.provider_config)
        report = build_deep_inspection_provider_probe(config)
    except (OSError, json.JSONDecodeError, DeepInspectionProviderConfigError) as exc:
        parser.error(str(exc))
    if args.out:
        path = write_deep_inspection_provider_probe(report, args.out)
        print(f"Deep-inspection provider probe report: {path}")
    print(f"Configured: {str(report['configured']).lower()}")
    print(f"Provider count: {report['provider_count']}")
    print(f"Missing requirements: {len(report['missing_requirements'])}")
    print("No inference run: true")
    print("Privacy: aggregate-only; no images, OCR text, paths, hashes, thumbnails, filenames, or row-level evidence.")
    return 0


def _main_deep_inspection_candidate_summary(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="archive-scan-qc deep-inspection-candidate-summary",
        description=(
            "Write an aggregate-only dry-run summary of already-detected QC targets that could be "
            "candidates for later optional deep inspection. No inference, OCR, image processing, "
            "directory scanning, provider subprocesses, or network calls are run."
        ),
    )
    parser.add_argument("--scan-report", default=None, type=Path, help="Optional scan_qc_report.json input.")
    parser.add_argument(
        "--processing-review-package",
        default=None,
        type=Path,
        help="Optional processing_review_package.json input.",
    )
    parser.add_argument(
        "--provider-probe",
        default=None,
        type=Path,
        help="Optional deep_inspection_provider_probe.json input for aggregate provider counts.",
    )
    parser.add_argument(
        "--provider-config",
        default=None,
        type=Path,
        help="Optional provider metadata config. It is parsed only for aggregate configured/provider counts.",
    )
    parser.add_argument("--out", required=True, type=Path, help="Output JSON path or directory.")
    args = parser.parse_args(argv)
    try:
        inputs = load_deep_inspection_candidate_inputs(
            scan_report_path=args.scan_report,
            processing_review_package_path=args.processing_review_package,
            provider_probe_path=args.provider_probe,
            provider_config_path=args.provider_config,
        )
        summary = build_deep_inspection_candidate_summary(**inputs)
        path = write_deep_inspection_candidate_summary(summary, args.out)
    except (OSError, ValueError, json.JSONDecodeError, DeepInspectionProviderConfigError) as exc:
        parser.error(str(exc))
    print(f"Deep-inspection candidate summary: {path}")
    print(f"Candidate total: {summary['candidate_total']}")
    print(f"Provider configured: {str(summary['provider_configured']).lower()}")
    print(f"Provider count: {summary['provider_count']}")
    print("No inference run: true")
    print(
        "Privacy: aggregate-only; no filenames, paths, hashes, OCR text, thumbnails, image content, "
        "row-level IDs, reviewer notes, manifests, derivative image references, or source roots."
    )
    return 0


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


def _main_review_decisions_verify(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="archive-scan-qc review-decisions-verify",
        description="Verify a browser-exported privacy-safe review decision summary.",
    )
    parser.add_argument("--summary", required=True, type=Path, help="Path to scan-qc-review-decisions.summary.json.")
    parser.add_argument("--out", required=True, type=Path, help="Output aggregate verification summary JSON path or directory.")
    args = parser.parse_args(argv)
    try:
        path, payload = write_review_decision_verification_summary(args.summary, args.out)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    decisions = payload["decision_summary"]
    print(f"Review decision verification summary: {path}")
    print(f"Verification status: {payload['status']}")
    print(f"Total decisions: {decisions['total_decisions']}")
    print(f"Completion status: {decisions['completion_status']}")
    print(f"Blocking items: {payload['blocking_count']}")
    print("Privacy: aggregate-only verifier output; private indicators are reported by code/count only.")
    return 0 if payload["status"] == "pass" else 1


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
        "--aggregate-baseline-summary",
        default=None,
        type=Path,
        help="Optional aggregate_baseline_summary.json from scripts/run_aggregate_baseline.py.",
    )
    parser.add_argument(
        "--main-aggregate-baseline-summary",
        default=None,
        type=Path,
        help="Optional latest-main aggregate_baseline_summary.json for PR-specific throughput regression diagnostics.",
    )
    parser.add_argument(
        "--acceptance-sampling-review",
        default=None,
        type=Path,
        help="Optional acceptance_sampling_review.json; only its aggregate_sampling_counts field is copied into the gate.",
    )
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
            aggregate_baseline_summary_path=args.aggregate_baseline_summary,
            main_aggregate_baseline_summary_path=args.main_aggregate_baseline_summary,
            acceptance_sampling_review_path=args.acceptance_sampling_review,
            min_scan_files_per_minute=args.min_scan_files_per_minute,
            min_processing_files_per_minute=args.min_processing_files_per_minute,
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f"Acceptance summary: {path}")
    print(f"Acceptance status: {payload['status']}")
    print("Sensitivity: aggregate-only summary; no filenames, paths, hashes, thumbnails, row-level findings, notes, OCR, or image content.")
    return 0 if payload["pass"] else 1


def _main_acceptance_sampling_export(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="archive-scan-qc acceptance-sampling-export",
        description="Write sensitive local acceptance sampling JSON and CSV from scan_qc_report.json.",
    )
    parser.add_argument("--report", required=True, type=Path, help="Path to scan_qc_report.json.")
    parser.add_argument("--out", required=True, type=Path, help="Output directory for acceptance sampling JSON and CSV.")
    parser.add_argument(
        "--sample-ratio",
        default=DEFAULT_SAMPLE_RATIO,
        type=float,
        help="Minimum deterministic sample ratio. Defaults to 0.05 and cannot be lower.",
    )
    args = parser.parse_args(argv)
    try:
        json_path, csv_path, payload = write_acceptance_sampling_export(args.report, args.out, sample_ratio=args.sample_ratio)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    counts = payload["aggregate_sampling_counts"]
    print(f"Acceptance sampling JSON: {json_path}")
    print(f"Acceptance sampling CSV: {csv_path}")
    print(f"Input records: {counts['input_total']}")
    print(f"Sampling target: {counts['target_sample_count']} records at {counts['target_sample_ratio']:.2%}")
    print(f"Generated sample tasks: {counts['generated_sample_task_count']}")
    print(f"Reviewed sample tasks: {counts['reviewed_sample_count']}")
    print(f"Sampling target met: {counts['sampling_target_met']}")
    print("Sensitivity: sensitive local row-level evidence; contains paths/hashes, no images, thumbnails, OCR text, or image bytes.")
    return 0


def _main_evidence_bundle_verify(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="archive-scan-qc evidence-bundle-verify",
        description="Verify aggregate-only release/handoff evidence without reading private images or row-level reports.",
    )
    parser.add_argument("--evidence-dir", required=True, type=Path, help="Directory containing aggregate JSON evidence.")
    parser.add_argument("--out", default=None, type=Path, help=f"Output JSON path or directory for {EVIDENCE_BUNDLE_JSON}.")
    args = parser.parse_args(argv)
    try:
        path, payload = write_evidence_bundle_summary(args.evidence_dir, args.out)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f"Aggregate evidence bundle summary: {path}")
    print(f"Evidence bundle status: {payload['status']}")
    print(f"Checks passed: {payload['checks_passed']}")
    print(f"Checks failed: {payload['checks_failed']}")
    print("Privacy: aggregate-only verifier output; private indicators are reported by code only, with source values omitted.")
    return 0 if payload["status"] == "pass" else 1


def _main_final_handoff_summary(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="archive-scan-qc final-handoff-summary",
        description="Write final public-safe handoff status JSON from aggregate evidence summaries only.",
    )
    parser.add_argument("--evidence-dir", required=True, type=Path, help="Directory containing aggregate evidence summary JSON.")
    parser.add_argument("--out", default=None, type=Path, help=f"Output JSON path or directory for {FINAL_HANDOFF_JSON}.")
    args = parser.parse_args(argv)
    try:
        path, payload = write_final_handoff_summary(args.evidence_dir, args.out)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f"Final production handoff summary: {path}")
    print(f"Handoff status: {payload['status']}")
    print(f"Ready for handoff: {payload['ready_for_handoff']}")
    print(f"Blocking items: {payload['blocking_item_count']}")
    print("Privacy: aggregate-only handoff summary; blockers are reported by aggregate code/count only.")
    return 0 if payload["ready_for_handoff"] else 1


def _main_public_safe_validation_index(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="archive-scan-qc public-safe-validation-index",
        description="Index known public-safe aggregate validation JSON outputs without reading private artifacts.",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input-dir", default=None, type=Path, help="Directory containing known public-safe aggregate JSON files.")
    source.add_argument(
        "--file",
        default=[],
        action="append",
        type=Path,
        help="Explicit known public-safe aggregate JSON file. Repeat for multiple files.",
    )
    parser.add_argument("--out", default=None, type=Path, help=f"Output JSON path or directory for {VALIDATION_INDEX_JSON}.")
    args = parser.parse_args(argv)
    try:
        path, payload = write_public_safe_validation_index(input_dir=args.input_dir, files=args.file, output_path=args.out)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f"Public-safe validation index: {path}")
    print(f"Validation index status: {payload['status']}")
    print(f"Artifacts present: {payload['summary']['artifacts_present']}")
    print(f"Checks passed: {payload['checks_passed']}")
    print(f"Checks failed: {payload['checks_failed']}")
    print("Privacy: public-safe aggregate index only; private indicators are reported by filename/category/code/count only.")
    return 0 if payload["status"] == "pass" else 1


def _main_private_validation_aggregate(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="archive-scan-qc private-validation-aggregate",
        description="Build a public-safe aggregate report from operator-approved private validation JSON inputs.",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input-dir", default=None, type=Path, help="Directory containing private validation JSON inputs.")
    source.add_argument(
        "--file",
        default=[],
        action="append",
        type=Path,
        help="Private validation JSON input. Repeat for multiple files.",
    )
    parser.add_argument("--out", default=None, type=Path, help=f"Output JSON path or directory for {PRIVATE_VALIDATION_AGGREGATE_JSON}.")
    args = parser.parse_args(argv)
    try:
        path, payload = write_private_validation_aggregate(input_dir=args.input_dir, files=args.file, output_path=args.out)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f"Private validation aggregate summary: {path}")
    print(f"Validation status: {payload['status']}")
    print(f"Validation groups: {payload['group_count']}")
    print("Privacy: public-safe aggregate metrics only; private labels, paths, filenames, hashes, OCR text, and images are omitted.")
    return 0 if payload["status"] == "pass" else 1


def _main_ocr_provider_probe(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="archive-scan-qc ocr-provider-probe",
        description="Probe a local OCR provider without running OCR on images.",
    )
    parser.add_argument("--provider", default="disabled", choices=("disabled", "tesseract"), help="Local OCR provider to probe.")
    parser.add_argument("--provider-command", default=None, help="Optional provider executable path for local-only probing.")
    parser.add_argument("--out", default=None, type=Path, help=f"Output JSON path or directory for {OCR_PROVIDER_PROBE_JSON}.")
    args = parser.parse_args(argv)
    try:
        path, payload = write_ocr_provider_probe(
            output_path=args.out,
            config=OcrProviderProbeConfig(provider=args.provider, command=args.provider_command),
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f"OCR provider probe: {path}")
    print(f"Provider status: {payload['status']}")
    print("Privacy: provider probe only; no images, OCR text, paths, filenames, hashes, or row-level results are written.")
    return 0 if payload["status"] in {"available", "disabled"} else 1


def _main_ocr_preprocessing_ocr_validation(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="archive-scan-qc ocr-preprocessing-ocr-validation",
        description="Run synthetic OCR preprocessing validation and write a public-safe aggregate summary.",
    )
    parser.add_argument("--out", required=True, type=Path, help=f"Output directory for {OCR_PREPROCESSING_OCR_VALIDATION_JSON}.")
    parser.add_argument("--provider", default="disabled", choices=("disabled", "tesseract"), help="Optional local OCR provider.")
    parser.add_argument("--provider-command", default=None, help="Optional provider executable path for local-only OCR validation.")
    parser.add_argument(
        "--require-ocr-metric",
        action="store_true",
        help="Fail if OCR CER/WER metrics cannot be produced by the selected local provider.",
    )
    parser.add_argument(
        "--min-cer-relative-reduction",
        default=0.25,
        type=_non_negative_float,
        help="Minimum source-to-processed CER reduction required when OCR metrics are available.",
    )
    parser.add_argument(
        "--min-wer-relative-reduction",
        default=0.0,
        type=_non_negative_float,
        help="Minimum source-to-processed WER reduction required when OCR metrics are available.",
    )
    args = parser.parse_args(argv)
    try:
        path, payload = write_ocr_preprocessing_ocr_validation(
            OcrValidationConfig(
                output_dir=args.out,
                provider=args.provider,
                provider_command=args.provider_command,
                require_ocr_metric=args.require_ocr_metric,
                min_cer_relative_reduction=args.min_cer_relative_reduction,
                min_wer_relative_reduction=args.min_wer_relative_reduction,
            )
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f"OCR preprocessing validation summary: {path}")
    print(f"Validation status: {payload['status']}")
    print(f"OCR metrics available: {payload['ocr_text_metrics']['available']}")
    print("Privacy: public-safe aggregate metrics only; OCR text, paths, filenames, hashes, images, and row-level records are omitted.")
    return 0 if payload["status"] == "pass" else 1


def _main_workbench_summary(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="archive-scan-qc workbench-summary",
        description=(
            "Bundle known public-safe aggregate JSON artifacts into one static-workbench input. "
            "Private reports, row-level evidence, images, manifests, logs, commands, and environment values are not read."
        ),
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--evidence-dir", default=None, type=Path, help="Directory containing known public-safe aggregate JSON files.")
    source.add_argument(
        "--file",
        default=[],
        action="append",
        type=Path,
        help="Explicit known public-safe aggregate JSON file. Repeat for multiple files.",
    )
    parser.add_argument("--out", default=None, type=Path, help=f"Output JSON path or directory for {WORKBENCH_SUMMARY_JSON}.")
    args = parser.parse_args(argv)
    try:
        path, payload = write_workbench_public_summary(evidence_dir=args.evidence_dir, files=args.file, output_path=args.out)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f"Workbench public summary: {path}")
    print(f"Workbench summary status: {payload['status']}")
    print(f"Ready: {payload['ready']}")
    print(f"Artifacts present: {payload['summary']['artifacts_present']}")
    print(f"Checks passed: {payload['checks_passed']}")
    print(f"Checks failed: {payload['checks_failed']}")
    print("Privacy: public-safe aggregate workbench input only; private inputs are rejected or reported by code/count only.")
    return 0 if payload["status"] == "pass" else 1


def _main_artifact_readiness_checklist(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="archive-scan-qc artifact-readiness-checklist",
        description=(
            "Generate a public-safe artifact readiness checklist from known aggregate JSON artifacts only. "
            "Private reports, images, row-level values, manifests, commands, notes, and environment values are not read."
        ),
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--evidence-dir", default=None, type=Path, help="Directory containing known aggregate/public-safe JSON files.")
    source.add_argument(
        "--file",
        default=[],
        action="append",
        type=Path,
        help="Explicit known aggregate/public-safe JSON file. Repeat for multiple files.",
    )
    parser.add_argument("--out", default=None, type=Path, help=f"Output JSON path or directory for {ARTIFACT_READINESS_JSON}.")
    args = parser.parse_args(argv)
    try:
        path, payload = write_artifact_readiness_checklist(evidence_dir=args.evidence_dir, files=args.file, output_path=args.out)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f"Artifact readiness checklist: {path}")
    print(f"Artifact readiness status: {payload['status']}")
    print(f"Ready: {payload['ready']}")
    print(f"Artifacts present: {payload['summary']['artifacts_present']}")
    print(f"Required missing: {payload['summary']['required_missing_count']}")
    print(f"Blocking items: {payload['summary']['blocking_count']}")
    print("Privacy: public-safe aggregate readiness only; private inputs are rejected or reported by code/count only.")
    return 0 if payload["ready"] else 1


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
    parser.add_argument("--scanner-gutter-trim", action="store_true", help="Plan conservative light scanner gutter trim candidates.")
    parser.add_argument("--despeckle", action="store_true", help="Plan isolated dark speckle cleanup candidates.")
    parser.add_argument("--normalize-tones", action="store_true", help="Plan conservative gray/dark page tone normalization candidates.")
    parser.add_argument(
        "--normalize-paper-color-cast",
        action="store_true",
        help="Plan conservative mild uniform scanner color-cast normalization candidates.",
    )
    parser.add_argument("--lighten-edge-shadow", action="store_true", help="Plan conservative narrow edge-shadow lightening candidates.")
    parser.add_argument("--lighten-corner-shadows", action="store_true", help="Plan conservative smooth corner-shadow cleanup candidates.")
    parser.add_argument("--lighten-background-stains", action="store_true", help="Plan conservative light background stain candidates.")
    parser.add_argument("--lighten-fold-shadows", action="store_true", help="Plan conservative narrow fold shadow cleanup candidates.")
    parser.add_argument("--level-illumination-gradient", action="store_true", help="Plan conservative smooth paper illumination-gradient candidates.")
    parser.add_argument("--clean-bleed-through", action="store_true", help="Plan conservative faint reverse-side ghost cleanup candidates.")
    parser.add_argument("--lighten-scanlines", action="store_true", help="Plan conservative low-contrast scanline lightening candidates.")
    parser.add_argument("--enhance-faded-text", action="store_true", help="Plan conservative low-contrast faded text enhancement candidates.")
    parser.add_argument("--sharpen-text-edges", action="store_true", help="Plan conservative blurred text edge sharpening candidates.")
    parser.add_argument(
        "--despeckle-backend",
        choices=("fallback", "numpy"),
        default="fallback",
        help="Despeckle planning backend. Defaults to conservative fallback; numpy is opt-in.",
    )
    parser.add_argument(
        "--reuse-scan-measurements",
        action="store_true",
        help="In safe cases, reuse measurements already present in the scan report while building the plan.",
    )
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
                scanner_gutter_trim=args.scanner_gutter_trim,
                despeckle=args.despeckle,
                normalize_tones=args.normalize_tones,
                normalize_paper_color_cast=args.normalize_paper_color_cast,
                lighten_edge_shadow=args.lighten_edge_shadow,
                lighten_corner_shadows=args.lighten_corner_shadows,
                lighten_background_stains=args.lighten_background_stains,
                lighten_fold_shadows=args.lighten_fold_shadows,
                clean_bleed_through=args.clean_bleed_through,
                lighten_scanlines=args.lighten_scanlines,
                enhance_faded_text=args.enhance_faded_text,
                sharpen_text_edges=args.sharpen_text_edges,
                despeckle_backend=args.despeckle_backend,
                reuse_scan_measurements=args.reuse_scan_measurements,
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


def _main_rework_action_list(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="archive-scan-qc rework-action-list",
        description="Write a sensitive local operator rework action list from QC findings and processing evidence.",
    )
    parser.add_argument("--report", required=True, type=Path, help="Path to scan_qc_report.json.")
    parser.add_argument(
        "--processing-audit-summary",
        default=None,
        type=Path,
        help="Optional aggregate processing_audit_summary.json.",
    )
    parser.add_argument(
        "--processing-retry-manifest",
        default=None,
        type=Path,
        help="Optional sensitive local processing_retry_manifest.json.",
    )
    parser.add_argument("--out", required=True, type=Path, help="Output JSON path for the local rework action list.")
    parser.add_argument("--csv-out", default=None, type=Path, help="Optional output CSV path for the local rework action list.")
    args = parser.parse_args(argv)
    try:
        json_path, csv_path, payload = write_rework_action_list(
            args.report,
            args.out,
            processing_audit_summary_path=args.processing_audit_summary,
            processing_retry_manifest_path=args.processing_retry_manifest,
            csv_path=args.csv_out,
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f"Rework action list JSON: {json_path}")
    if csv_path:
        print(f"Rework action list CSV: {csv_path}")
    print(f"Actions: {payload['summary']['total_actions']}")
    print("Sensitivity: LOCAL-ONLY sensitive row-level evidence; contains paths/hashes/messages, no thumbnails or image content.")
    return 0


def _main_production_review_queue(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="archive-scan-qc production-review-queue",
        description="Write a local-only Chinese operator production review queue from scan QC and processing artifacts.",
    )
    parser.add_argument("--scan-qc-report", default=None, type=Path, help="Optional path to scan_qc_report.json.")
    parser.add_argument(
        "--processing-review-package",
        default=None,
        type=Path,
        help="Optional path to processing_review_package.json.",
    )
    parser.add_argument("--rework-action-list", default=None, type=Path, help="Optional path to rework_action_list.json.")
    parser.add_argument(
        "--out",
        required=True,
        type=Path,
        help=f"Output JSON path for {PRODUCTION_REVIEW_QUEUE_JSON}.",
    )
    args = parser.parse_args(argv)
    try:
        json_path, payload = write_production_review_queue(
            args.out,
            scan_qc_report_path=args.scan_qc_report,
            processing_review_package_path=args.processing_review_package,
            rework_action_list_path=args.rework_action_list,
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f"Production review queue JSON: {json_path}")
    print(f"Queue items: {payload['summary']['total_items']}")
    print("Sensitivity: LOCAL-ONLY production queue; no images/base64/OCR text/hashes are embedded.")
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


def _main_batch_rename_plan(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="archive-scan-qc batch-rename-plan",
        description="Dry-run a local batch rename mapping and write JSON/CSV/XLSX logs.",
    )
    parser.add_argument("--input", required=True, type=Path, help="Root directory containing files to rename.")
    parser.add_argument(
        "--mapping-csv",
        required=True,
        type=Path,
        help="CSV with source_relative_path and target_relative_path/new_relative_path columns.",
    )
    parser.add_argument("--out", required=True, type=Path, help="Output directory for the rename plan logs.")
    args = parser.parse_args(argv)
    try:
        json_path, csv_path, xlsx_path, payload = write_batch_rename_plan(args.input, args.mapping_csv, args.out)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f"Batch rename plan JSON: {json_path}")
    print(f"Batch rename plan CSV: {csv_path}")
    print(f"Batch rename plan Excel: {xlsx_path}")
    print(f"Status: {payload['status']}")
    print(f"Rows ready: {payload['summary']['ready_count']}")
    print(f"Rows blocked: {payload['summary']['blocking_count']}")
    print("Sensitivity: LOCAL-ONLY path-bearing rename plan; not public-safe.")
    return 0 if payload["status"] == "ready" else 1


def _main_batch_rename_apply(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="archive-scan-qc batch-rename-apply",
        description="Apply a local batch rename plan and write apply plus rollback manifests.",
    )
    parser.add_argument(
        "--plan-json",
        required=True,
        type=Path,
        help=f"Path to {BATCH_RENAME_PLAN_JSON} written by batch-rename-plan.",
    )
    parser.add_argument(
        "--out",
        default=None,
        type=Path,
        help=f"Optional output directory for {BATCH_RENAME_APPLY_JSON}; defaults to the plan directory.",
    )
    args = parser.parse_args(argv)
    try:
        json_path, csv_path, xlsx_path, rollback_path, payload = apply_batch_rename_plan(args.plan_json, args.out)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f"Batch rename apply JSON: {json_path}")
    print(f"Batch rename apply CSV: {csv_path}")
    print(f"Batch rename apply Excel: {xlsx_path}")
    print(f"Batch rename rollback manifest: {rollback_path}")
    print(f"Rows applied: {payload['summary']['applied_count']}")
    print("Sensitivity: LOCAL-ONLY path-bearing rename logs; not public-safe.")
    return 0


def _main_case_split_plan(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="archive-scan-qc case-split-plan",
        description="Dry-run copying page ranges into case folders from a CSV/XLSX case map.",
    )
    parser.add_argument("--input", required=True, type=Path, help="Root directory containing source page images.")
    parser.add_argument(
        "--case-map",
        required=True,
        type=Path,
        help="CSV/XLSX with case_name, start_page, and end_page columns.",
    )
    parser.add_argument("--target", required=True, type=Path, help="Destination root for case folders.")
    parser.add_argument("--out", required=True, type=Path, help="Output directory for case split plan logs.")
    args = parser.parse_args(argv)
    try:
        json_path, csv_path, xlsx_path, payload = write_case_split_plan(
            args.input,
            args.case_map,
            args.target,
            args.out,
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f"Case split plan JSON: {json_path}")
    print(f"Case split plan CSV: {csv_path}")
    print(f"Case split plan Excel: {xlsx_path}")
    print(f"Status: {payload['status']}")
    print(f"Cases ready: {payload['summary']['ready_count']}")
    print(f"Cases blocked: {payload['summary']['blocking_count']}")
    print(f"Planned copied files: {payload['summary']['planned_copy_count']}")
    print("Sensitivity: LOCAL-ONLY path-bearing case split plan; not public-safe.")
    return 0 if payload["status"] == "ready" else 1


def _main_case_split_apply(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="archive-scan-qc case-split-apply",
        description="Apply a local case split plan and write apply plus rollback manifests.",
    )
    parser.add_argument(
        "--plan-json",
        required=True,
        type=Path,
        help=f"Path to {CASE_SPLIT_PLAN_JSON} written by case-split-plan.",
    )
    parser.add_argument(
        "--out",
        default=None,
        type=Path,
        help=f"Optional output directory for {CASE_SPLIT_APPLY_JSON}; defaults to the plan directory.",
    )
    args = parser.parse_args(argv)
    try:
        json_path, csv_path, xlsx_path, rollback_path, payload = apply_case_split_plan(args.plan_json, args.out)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f"Case split apply JSON: {json_path}")
    print(f"Case split apply CSV: {csv_path}")
    print(f"Case split apply Excel: {xlsx_path}")
    print(f"Case split rollback manifest: {rollback_path}")
    print(f"Cases applied: {payload['summary']['applied_case_count']}")
    print(f"Files copied: {payload['summary']['copied_file_count']}")
    print("Sensitivity: LOCAL-ONLY path-bearing case split logs; not public-safe.")
    return 0


def _validate_processing_flags(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.auto_crop and not args.process_out:
        parser.error("--auto-crop requires --process-out")
    if args.deskew and not args.process_out:
        parser.error("--deskew requires --process-out")
    if args.trim_dark_border and not args.process_out:
        parser.error("--trim-dark-border requires --process-out")
    if args.scanner_gutter_trim and not args.process_out:
        parser.error("--scanner-gutter-trim requires --process-out")
    if args.despeckle and not args.process_out:
        parser.error("--despeckle requires --process-out")
    if args.normalize_tones and not args.process_out:
        parser.error("--normalize-tones requires --process-out")
    if args.normalize_paper_color_cast and not args.process_out:
        parser.error("--normalize-paper-color-cast requires --process-out")
    if args.lighten_edge_shadow and not args.process_out:
        parser.error("--lighten-edge-shadow requires --process-out")
    if args.lighten_corner_shadows and not args.process_out:
        parser.error("--lighten-corner-shadows requires --process-out")
    if args.lighten_background_stains and not args.process_out:
        parser.error("--lighten-background-stains requires --process-out")
    if args.lighten_fold_shadows and not args.process_out:
        parser.error("--lighten-fold-shadows requires --process-out")
    if args.level_illumination_gradient and not args.process_out:
        parser.error("--level-illumination-gradient requires --process-out")
    if args.clean_bleed_through and not args.process_out:
        parser.error("--clean-bleed-through requires --process-out")
    if args.lighten_scanlines and not args.process_out:
        parser.error("--lighten-scanlines requires --process-out")
    if args.enhance_faded_text and not args.process_out:
        parser.error("--enhance-faded-text requires --process-out")
    if args.sharpen_text_edges and not args.process_out:
        parser.error("--sharpen-text-edges requires --process-out")
    if args.resume_processing and not args.process_out:
        parser.error("--resume-processing requires --process-out")


def _load_rules_profile(parser: argparse.ArgumentParser, args: argparse.Namespace):
    try:
        rules_profile = load_rules_profile_selection(args.rules_profile, getattr(args, "rule_template", None))
    except RulesProfileError as exc:
        parser.error(str(exc))
    if rules_profile and args.min_dpi is not None:
        rules_profile = replace(rules_profile, min_dpi=args.min_dpi)
    if rules_profile and getattr(args, "dpi_purpose", None) is not None:
        rules_profile = replace(rules_profile, dpi_purpose=args.dpi_purpose)
    if rules_profile and args.name_pattern is not None:
        rules_profile = replace(rules_profile, name_pattern=args.name_pattern)
    return rules_profile


def _load_rules_profile_for_preflight(args: argparse.Namespace):
    if not args.rules_profile and not getattr(args, "rule_template", None):
        return None, None
    try:
        return load_rules_profile_selection(args.rules_profile, getattr(args, "rule_template", None)), None
    except RulesProfileError as exc:
        return None, str(exc)


def _apply_rule_template_processing_defaults(args: argparse.Namespace) -> None:
    template_id = getattr(args, "rule_template", None)
    if not template_id:
        return
    try:
        defaults = processing_defaults_for_rule_template(template_id)
    except RulesProfileError:
        return
    for field, enabled in defaults.items():
        if hasattr(args, field) or field in {"despeckle_content_type_check", "ocr_preprocess", "ocr_binary"}:
            setattr(args, field, enabled)


def _processing_profile_for_args(args: argparse.Namespace) -> str:
    template_id = getattr(args, "rule_template", None)
    try:
        return processing_profile_for_rule_template(template_id)
    except RulesProfileError:
        return "standard"


if __name__ == "__main__":
    raise SystemExit(main())
