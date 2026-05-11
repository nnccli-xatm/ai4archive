"""Production run-plan orchestration for multiple archive scan batches."""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from typing import Any

from .preflight import PreflightConfig, run_preflight, write_preflight_report
from .processing import ProcessingOptions, process_images
from .reports import write_reports
from .rules import RulesProfileError, load_rules_profile
from .scanner import ScanConfig, scan_batch


RUN_PLAN_JSON = "run_plan_summary.json"
RUN_PLAN_CSV = "run_plan_summary.csv"
REQUIRED_FIELDS = ("batch_id", "input_dir")


@dataclass(frozen=True)
class PlanBatch:
    batch_id: str
    input_dir: Path
    report_dir: Path
    process_out: Path | None
    manifest_csv: Path | None
    rules_profile: Path | None
    workers: int | None
    min_dpi: int | None
    name_pattern: str | None
    auto_crop: bool
    deskew: bool
    trim_dark_border: bool
    despeckle: bool
    resume_processing: bool


@dataclass(frozen=True)
class RunPlan:
    project_id: str
    batches: list[PlanBatch]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="archive-scan-qc run-plan",
        description="Run preflight, scan, and optional processing for multiple production batches.",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--plan-csv", type=Path, help="CSV plan with one batch per row.")
    source.add_argument("--plan-json", type=Path, help="JSON plan with a batches list or top-level list.")
    parser.add_argument("--out", required=True, type=Path, help="Project-level output root.")
    parser.add_argument("--project", default="default-project", help="Project identifier for all batches.")
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Continue remaining batches after a preflight, scan, or processing failure.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        plan = load_run_plan(args)
        summary = run_plan(plan, args.out, continue_on_error=args.continue_on_error)
    except (OSError, ValueError, RulesProfileError) as exc:
        parser.exit(2, f"archive-scan-qc run-plan: error: {exc}\n")

    print(f"Run plan batches: {summary['summary']['total_batches']}")
    print(f"Passed batches: {summary['summary']['passed_batches']}")
    print(f"Failed batches: {summary['summary']['failed_batches']}")
    print(f"P0 findings: {summary['summary']['p0_findings']}")
    print(f"Processing failed files: {summary['summary']['processing_failed_files']}")
    print(f"Preflight errors: {summary['summary']['preflight_error_count']}")
    print(f"Run plan summary: {args.out / RUN_PLAN_JSON}")
    print(f"Run plan CSV: {args.out / RUN_PLAN_CSV}")
    return 0 if summary["summary"]["failed_batches"] == 0 else 1


def load_run_plan(args: argparse.Namespace) -> RunPlan:
    output_root = args.out.resolve()
    if args.plan_csv:
        rows = _load_csv_rows(args.plan_csv)
        plan_dir = args.plan_csv.resolve().parent
    else:
        rows, json_project = _load_json_rows(args.plan_json)
        plan_dir = args.plan_json.resolve().parent
        if args.project == "default-project" and json_project:
            args.project = json_project

    batches = [_batch_from_row(row, index, plan_dir, output_root) for index, row in enumerate(rows, start=1)]
    if not batches:
        raise ValueError("Run plan must contain at least one batch.")
    batch_ids = [batch.batch_id for batch in batches]
    duplicates = sorted({batch_id for batch_id in batch_ids if batch_ids.count(batch_id) > 1})
    if duplicates:
        raise ValueError(f"Run plan contains duplicate batch_id values: {', '.join(duplicates)}.")
    return RunPlan(project_id=args.project, batches=batches)


def run_plan(plan: RunPlan, output_root: Path, *, continue_on_error: bool) -> dict[str, Any]:
    output_root = output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now(timezone.utc)
    start_seconds = time.perf_counter()
    batch_summaries: list[dict[str, Any]] = []

    for index, batch in enumerate(plan.batches, start=1):
        result = _run_batch(plan.project_id, batch, index)
        batch_summaries.append(result)
        summary = _build_summary(plan.project_id, started_at, start_seconds, batch_summaries, finished=False)
        _write_summary(summary, output_root)
        if result["status"] == "failed" and not continue_on_error:
            break

    final = _build_summary(plan.project_id, started_at, start_seconds, batch_summaries, finished=True)
    _write_summary(final, output_root)
    return final


def _run_batch(project_id: str, batch: PlanBatch, index: int) -> dict[str, Any]:
    base = {
        "batch_index": index,
        "batch_id": batch.batch_id,
        "status": "failed",
        "failure_stage": None,
        "failure_reason": None,
        "report_dir": _public_output(batch.report_dir),
        "process_out": _public_output(batch.process_out),
        "preflight_status": None,
        "preflight_error_count": 0,
        "preflight_warning_count": 0,
        "total_files": 0,
        "openable_files": 0,
        "total_findings": 0,
        "p0_findings": 0,
        "p1_findings": 0,
        "p2_findings": 0,
        "manifest_sequence_invalid_count": 0,
        "manifest_sequence_duplicate_count": 0,
        "manifest_sequence_gap_count": 0,
        "manifest_sequence_order_mismatch_count": 0,
        "processing_enabled": batch.process_out is not None,
        "processing_failed_files": 0,
        "processing_processed_files": 0,
        "processing_resumed_files": 0,
        "processing_duplicate_reused_files": 0,
        "processing_existing_derivative_reused_files": 0,
        "scan_elapsed_seconds": 0.0,
        "scan_files_per_minute": 0.0,
        "processing_elapsed_seconds": 0.0,
        "processing_files_per_minute": 0.0,
        "processing_operation_timings": {},
        "workers": batch.workers,
    }
    try:
        rules_profile, rules_profile_error = _load_rules_profile_for_preflight(batch.rules_profile)
        preflight = run_preflight(
            PreflightConfig(
                project_id=project_id,
                batch_id=batch.batch_id,
                input_dir=batch.input_dir,
                output_dir=batch.report_dir,
                process_out=batch.process_out,
                manifest_csv=batch.manifest_csv,
                rules_profile=rules_profile,
                rules_profile_error=rules_profile_error,
                rules_profile_provided=batch.rules_profile is not None,
                workers=batch.workers,
                auto_crop=batch.auto_crop,
                deskew=batch.deskew,
                trim_dark_border=batch.trim_dark_border,
                despeckle=batch.despeckle,
                resume_processing=batch.resume_processing,
            )
        )
        write_preflight_report(preflight, batch.report_dir)
        base.update(
            {
                "preflight_status": preflight["status"],
                "preflight_error_count": len(preflight["errors"]),
                "preflight_warning_count": len(preflight["warnings"]),
                "manifest_sequence_invalid_count": preflight["manifest"]["sequence_invalid_count"],
                "manifest_sequence_duplicate_count": preflight["manifest"]["sequence_duplicate_count"],
                "manifest_sequence_gap_count": preflight["manifest"]["sequence_gap_count"],
                "manifest_sequence_order_mismatch_count": preflight["manifest"]["sequence_order_mismatch_count"],
            }
        )
        if preflight["status"] != "pass":
            base["failure_stage"] = "preflight"
            base["failure_reason"] = "preflight failed"
            return base

        rules_profile = _load_rules_profile(batch)
        report = scan_batch(
            ScanConfig(
                project_id=project_id,
                batch_id=batch.batch_id,
                input_dir=batch.input_dir,
                output_dir=batch.report_dir,
                min_dpi=batch.min_dpi if batch.min_dpi is not None else 200,
                name_pattern=batch.name_pattern,
                manifest_csv=batch.manifest_csv,
                rules_profile=rules_profile,
                workers=batch.workers,
            )
        )
        write_reports(report, batch.report_dir)
        scan_performance = report["summary"]["performance"]
        base.update(
            {
                "total_files": report["summary"]["total_files"],
                "openable_files": report["summary"]["openable_files"],
                "total_findings": report["summary"]["total_findings"],
                "p0_findings": report["summary"]["p0_findings"],
                "p1_findings": report["summary"]["p1_findings"],
                "p2_findings": report["summary"]["p2_findings"],
                "manifest_sequence_invalid_count": report["summary"]["manifest_sequence_invalid_count"],
                "manifest_sequence_duplicate_count": report["summary"]["manifest_sequence_duplicate_count"],
                "manifest_sequence_gap_count": report["summary"]["manifest_sequence_gap_count"],
                "manifest_sequence_order_mismatch_count": report["summary"]["manifest_sequence_order_mismatch_count"],
                "scan_elapsed_seconds": scan_performance["elapsed_seconds"],
                "scan_files_per_minute": scan_performance["files_per_minute"],
                "workers": scan_performance["effective_workers"],
            }
        )

        if batch.process_out:
            processing_manifest = process_images(
                report,
                batch.input_dir,
                batch.process_out,
                ProcessingOptions(
                    auto_crop=batch.auto_crop,
                    deskew=batch.deskew,
                    trim_dark_border=batch.trim_dark_border,
                    despeckle=batch.despeckle,
                    resume_processing=batch.resume_processing,
                    workers=batch.workers,
                ),
            )
            processing_summary = processing_manifest["summary"]
            processing_performance = processing_summary["performance"]
            base.update(
                {
                    "processing_failed_files": processing_summary["failed_files"],
                    "processing_processed_files": processing_summary["processed_files"],
                    "processing_resumed_files": processing_summary["resumed_files"],
                    "processing_duplicate_reused_files": processing_summary.get("duplicate_reused_files", 0),
                    "processing_existing_derivative_reused_files": processing_summary.get("existing_derivative_reused_files", 0),
                    "processing_elapsed_seconds": processing_performance["elapsed_seconds"],
                    "processing_files_per_minute": processing_performance["processed_files_per_minute"],
                    "processing_operation_timings": processing_performance.get("operation_timings", {}),
                }
            )

        if base["p0_findings"] or base["processing_failed_files"]:
            base["failure_stage"] = "scan" if base["p0_findings"] else "processing"
            base["failure_reason"] = "P0 findings present" if base["p0_findings"] else "processing failures present"
            return base

        base["status"] = "passed"
        return base
    except (OSError, ValueError, RulesProfileError) as exc:
        base["failure_stage"] = base["failure_stage"] or "run"
        base["failure_reason"] = str(exc)
        return base


def _build_summary(
    project_id: str,
    started_at: datetime,
    start_seconds: float,
    batches: list[dict[str, Any]],
    *,
    finished: bool,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    totals = {
        "total_batches": len(batches),
        "passed_batches": sum(1 for batch in batches if batch["status"] == "passed"),
        "failed_batches": sum(1 for batch in batches if batch["status"] == "failed"),
        "preflight_error_count": sum(int(batch["preflight_error_count"]) for batch in batches),
        "preflight_warning_count": sum(int(batch["preflight_warning_count"]) for batch in batches),
        "total_files": sum(int(batch["total_files"]) for batch in batches),
        "openable_files": sum(int(batch["openable_files"]) for batch in batches),
        "total_findings": sum(int(batch["total_findings"]) for batch in batches),
        "p0_findings": sum(int(batch["p0_findings"]) for batch in batches),
        "p1_findings": sum(int(batch["p1_findings"]) for batch in batches),
        "p2_findings": sum(int(batch["p2_findings"]) for batch in batches),
        "manifest_sequence_invalid_count": sum(int(batch["manifest_sequence_invalid_count"]) for batch in batches),
        "manifest_sequence_duplicate_count": sum(int(batch["manifest_sequence_duplicate_count"]) for batch in batches),
        "manifest_sequence_gap_count": sum(int(batch["manifest_sequence_gap_count"]) for batch in batches),
        "manifest_sequence_order_mismatch_count": sum(
            int(batch["manifest_sequence_order_mismatch_count"]) for batch in batches
        ),
        "processing_failed_files": sum(int(batch["processing_failed_files"]) for batch in batches),
        "processing_processed_files": sum(int(batch["processing_processed_files"]) for batch in batches),
        "processing_resumed_files": sum(int(batch["processing_resumed_files"]) for batch in batches),
        "processing_duplicate_reused_files": sum(int(batch["processing_duplicate_reused_files"]) for batch in batches),
        "processing_existing_derivative_reused_files": sum(
            int(batch["processing_existing_derivative_reused_files"]) for batch in batches
        ),
    }
    scan_elapsed = round(sum(float(batch["scan_elapsed_seconds"]) for batch in batches), 6)
    processing_elapsed = round(sum(float(batch["processing_elapsed_seconds"]) for batch in batches), 6)
    operation_timings = _aggregate_processing_operation_timings(batches)
    elapsed = max(0.0, round(time.perf_counter() - start_seconds, 6))
    return {
        "schema_version": "scan-qc.run-plan-summary.v1",
        "generated_at": now.isoformat(),
        "started_at": started_at.isoformat(),
        "finished_at": now.isoformat() if finished else None,
        "project_id": project_id,
        "privacy": {
            "aggregate_only": True,
            "omits": [
                "source filenames",
                "source relative paths",
                "source absolute paths",
                "content hashes",
                "row-level file metadata",
                "thumbnails",
                "image_content",
            ],
        },
        "summary": {
            **totals,
            "elapsed_seconds": elapsed,
            "scan_elapsed_seconds": scan_elapsed,
            "processing_elapsed_seconds": processing_elapsed,
            "scan_files_per_minute": _files_per_minute(totals["total_files"], scan_elapsed),
            "scan_openable_files_per_minute": _files_per_minute(totals["openable_files"], scan_elapsed),
            "processing_files_per_minute": _files_per_minute(totals["processing_processed_files"], processing_elapsed),
            "processing_operation_timings": operation_timings,
            "failed_batch_ids": [batch["batch_id"] for batch in batches if batch["status"] == "failed"],
        },
        "batches": batches,
    }


def _aggregate_processing_operation_timings(batches: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    operation_names = ["auto_crop", "deskew", "trim_dark_border", "despeckle"]
    totals: dict[str, dict[str, Any]] = {}
    for operation in operation_names:
        elapsed_seconds = 0.0
        file_count = 0
        enabled = False
        for batch in batches:
            batch_timings = batch.get("processing_operation_timings")
            if not isinstance(batch_timings, dict):
                continue
            timing = batch_timings.get(operation)
            if not isinstance(timing, dict):
                continue
            enabled = enabled or timing.get("enabled") is True
            if isinstance(timing.get("elapsed_seconds"), int | float):
                elapsed_seconds += float(timing["elapsed_seconds"])
            if isinstance(timing.get("file_count"), int):
                file_count += int(timing["file_count"])
        elapsed_seconds = round(elapsed_seconds, 6)
        totals[operation] = {
            "enabled": enabled,
            "file_count": file_count,
            "elapsed_seconds": elapsed_seconds,
            "files_per_minute": _files_per_minute(file_count, elapsed_seconds),
            "average_seconds_per_file": round(elapsed_seconds / file_count, 6) if file_count else None,
        }
        if operation == "despeckle":
            totals[operation].update(_aggregate_despeckle_backend_timings(batches, enabled))
    return totals


def _aggregate_despeckle_backend_timings(batches: list[dict[str, Any]], enabled: bool) -> dict[str, Any]:
    backend_counts = {"numpy": 0, "fallback": 0, "not_applicable": 0, "unknown": 0}
    for batch in batches:
        batch_timings = batch.get("processing_operation_timings")
        if not isinstance(batch_timings, dict):
            continue
        timing = batch_timings.get("despeckle")
        if not isinstance(timing, dict):
            continue
        counts = timing.get("backend_counts")
        if not isinstance(counts, dict):
            continue
        for mode in backend_counts:
            value = counts.get(mode)
            if isinstance(value, int):
                backend_counts[mode] += value

    active_modes = [mode for mode in ("numpy", "fallback", "not_applicable", "unknown") if backend_counts[mode]]
    if not enabled:
        backend_mode = "disabled"
    elif len(active_modes) == 1:
        backend_mode = active_modes[0]
    elif active_modes:
        backend_mode = "mixed"
    else:
        backend_mode = "unknown"
    return {
        "backend_mode": backend_mode,
        "numpy_available": backend_counts["numpy"] > 0,
        "backend_counts": backend_counts,
    }


def _write_summary(payload: dict[str, Any], output_root: Path) -> None:
    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / RUN_PLAN_JSON).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    fields = [
        "batch_index",
        "batch_id",
        "status",
        "failure_stage",
        "preflight_status",
        "preflight_error_count",
        "total_files",
        "openable_files",
        "p0_findings",
        "p1_findings",
        "p2_findings",
        "manifest_sequence_invalid_count",
        "manifest_sequence_duplicate_count",
        "manifest_sequence_gap_count",
        "manifest_sequence_order_mismatch_count",
        "processing_enabled",
        "processing_failed_files",
        "processing_processed_files",
        "processing_resumed_files",
        "processing_duplicate_reused_files",
        "processing_existing_derivative_reused_files",
        "scan_elapsed_seconds",
        "scan_files_per_minute",
        "processing_elapsed_seconds",
        "processing_files_per_minute",
        "report_dir",
        "process_out",
    ]
    with (output_root / RUN_PLAN_CSV).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(payload["batches"])


def _load_csv_rows(path: Path) -> list[dict[str, Any]]:
    try:
        with path.open("r", newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                raise ValueError("Run plan CSV must include a header row.")
            return [dict(row) for row in reader]
    except OSError as exc:
        raise ValueError(f"Run plan CSV could not be read: {path}") from exc


def _load_json_rows(path: Path) -> tuple[list[dict[str, Any]], str | None]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Run plan JSON is invalid at line {exc.lineno}, column {exc.colno}: {exc.msg}") from exc
    except OSError as exc:
        raise ValueError(f"Run plan JSON could not be read: {path}") from exc
    project_id = None
    if isinstance(payload, dict):
        project_id = str(payload["project_id"]) if payload.get("project_id") else None
        rows = payload.get("batches")
    else:
        rows = payload
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError("Run plan JSON must be a list of batch objects or an object with a batches list.")
    return rows, project_id


def _batch_from_row(row: dict[str, Any], index: int, plan_dir: Path, output_root: Path) -> PlanBatch:
    normalized = {str(key).strip(): value for key, value in row.items()}
    for field in REQUIRED_FIELDS:
        if not _text(normalized.get(field)):
            raise ValueError(f"Run plan row {index} missing required field '{field}'.")
    batch_id = _text(normalized["batch_id"])
    report_value = _text(normalized.get("report_dir") or normalized.get("report_name") or batch_id)
    process_value = _text(normalized.get("process_out"))
    return PlanBatch(
        batch_id=batch_id,
        input_dir=_plan_path(_text(normalized["input_dir"]), plan_dir),
        report_dir=_output_path(report_value, output_root),
        process_out=_output_path(process_value, output_root) if process_value else None,
        manifest_csv=_plan_path(_text(normalized.get("manifest_csv")), plan_dir) if _text(normalized.get("manifest_csv")) else None,
        rules_profile=_plan_path(_text(normalized.get("rules_profile")), plan_dir) if _text(normalized.get("rules_profile")) else None,
        workers=_optional_positive_int(normalized.get("workers"), "workers", index),
        min_dpi=_optional_positive_int(normalized.get("min_dpi"), "min_dpi", index),
        name_pattern=_text(normalized.get("name_pattern")) or None,
        auto_crop=_bool(normalized.get("auto_crop"), "auto_crop", index),
        deskew=_bool(normalized.get("deskew"), "deskew", index),
        trim_dark_border=_bool(normalized.get("trim_dark_border"), "trim_dark_border", index),
        despeckle=_bool(normalized.get("despeckle"), "despeckle", index),
        resume_processing=_bool(normalized.get("resume_processing"), "resume_processing", index),
    )


def _load_rules_profile(batch: PlanBatch):
    if not batch.rules_profile:
        return None
    profile = load_rules_profile(batch.rules_profile)
    if batch.min_dpi is not None:
        profile = replace(profile, min_dpi=batch.min_dpi)
    if batch.name_pattern is not None:
        profile = replace(profile, name_pattern=batch.name_pattern)
    return profile


def _load_rules_profile_for_preflight(path: Path | None):
    if not path:
        return None, None
    try:
        return load_rules_profile(path), None
    except RulesProfileError:
        return None, "Rules profile could not be loaded or validated."


def _plan_path(value: str, plan_dir: Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else plan_dir / path


def _output_path(value: str, output_root: Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else output_root / path


def _public_output(path: Path | None) -> str | None:
    return path.name if path else None


def _optional_positive_int(value: Any, field: str, index: int) -> int | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = int(text)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Run plan row {index} field '{field}' must be a positive integer.") from exc
    if parsed < 1:
        raise ValueError(f"Run plan row {index} field '{field}' must be a positive integer.")
    return parsed


def _bool(value: Any, field: str, index: int) -> bool:
    if isinstance(value, bool):
        return value
    text = _text(value).lower()
    if not text:
        return False
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"Run plan row {index} field '{field}' must be a boolean.")


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _files_per_minute(file_count: int, elapsed_seconds: float) -> float:
    if elapsed_seconds <= 0:
        return 0.0
    return round((file_count / elapsed_seconds) * 60, 2)
