"""Run an external CLI validation on the UCI NoisyOffice dataset.

The script treats ``archive_scan_qc`` as an external command and evaluates the
processed images against NoisyOffice clean grayscale ground truth.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterable
from urllib.request import urlretrieve
from zipfile import ZipFile

from PIL import Image, ImageDraw

try:
    import numpy as np
except ImportError as exc:  # pragma: no cover - environment preflight.
    raise SystemExit("NoisyOffice external CLI test requires numpy. Install with: python -m pip install numpy") from exc


SCHEMA_VERSION = "scan-qc.noisyoffice-external-cli-test.v1"
OCR_QUALITY_SUMMARY_SCHEMA_VERSION = "scan-qc.ocr-preprocessing-quality-summary.v1"
DEFAULT_DATA_ROOT = Path(r"D:\data-opt\NoisyOffice")
DEFAULT_OUTPUT_ROOT = Path("generated") / "noisyoffice_external_cli_test"
DEFAULT_DOC_REPORT = Path("docs") / "noisyoffice-external-cli-test-report.md"
NOISYOFFICE_URL = "https://archive.ics.uci.edu/static/public/318/noisyoffice.zip"
RULE_TEMPLATE_CHOICES = (
    "dat-31-2017-standard",
    "text-clean-print",
    "high-fidelity-original",
    "print-clean-v1",
    "ocr-preprocess-light-v1",
    "ocr-preprocess-v1",
)


@dataclass(frozen=True)
class ImagePair:
    source_path: Path
    clean_path: Path
    relative_path: str
    clean_id: str
    noise_type: str


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", default=DEFAULT_DATA_ROOT, type=Path, help="NoisyOffice dataset root.")
    parser.add_argument("--output-root", default=DEFAULT_OUTPUT_ROOT, type=Path, help="Generated test artifact root.")
    parser.add_argument("--report-path", default=DEFAULT_DOC_REPORT, type=Path, help="Markdown report path.")
    parser.add_argument("--python", default=sys.executable, help="Python executable used for external CLI calls.")
    parser.add_argument("--workers", default=min(4, os.cpu_count() or 1), type=_positive_int, help="CLI worker count.")
    parser.add_argument(
        "--rule-template",
        default="text-clean-print",
        choices=RULE_TEMPLATE_CHOICES,
        help="Rule template used for production-run.",
    )
    parser.add_argument(
        "--enforce-ocr-quality-gate",
        action="store_true",
        help="Fail the script when OCR preprocessing quality thresholds are not met.",
    )
    parser.add_argument(
        "--min-psnr-delta-db",
        default=1.0,
        type=float,
        help="Minimum macro PSNR improvement required by the OCR quality gate.",
    )
    parser.add_argument(
        "--min-ssim-delta",
        default=0.015,
        type=float,
        help="Minimum macro SSIM improvement required by the OCR quality gate.",
    )
    parser.add_argument(
        "--min-mse-reduction-ratio",
        default=0.10,
        type=float,
        help="Minimum macro MSE reduction ratio required by the OCR quality gate.",
    )
    parser.add_argument(
        "--min-mae-reduction-ratio",
        default=0.05,
        type=float,
        help="Minimum macro MAE reduction ratio required by the OCR quality gate.",
    )
    parser.add_argument(
        "--min-dark-f1-delta",
        default=0.0,
        type=float,
        help="Minimum macro dark-pixel F1 change required by the OCR quality gate.",
    )
    parser.add_argument(
        "--min-foreground-retention-delta",
        default=-0.002,
        type=float,
        help="Minimum macro foreground-retention change required by the OCR quality gate.",
    )
    parser.add_argument(
        "--min-positive-noise-groups",
        default=3,
        type=_positive_int,
        help="Minimum NoisyOffice noise groups with positive PSNR or SSIM delta.",
    )
    parser.add_argument("--run-id", default=None, help="Optional stable run id. Defaults to a UTC timestamp.")
    parser.add_argument("--download-url", default=NOISYOFFICE_URL, help="Official NoisyOffice ZIP URL.")
    parser.add_argument("--no-download", action="store_true", help="Fail if the ZIP is missing instead of downloading.")
    parser.add_argument("--no-doc-report", action="store_true", help="Only write generated artifacts.")
    parser.add_argument(
        "--synthetic-smoke",
        action="store_true",
        help="Create a tiny NoisyOffice-shaped synthetic dataset under --data-root for CI smoke validation.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    started_at = datetime.now(timezone.utc)
    run_id = args.run_id or started_at.strftime("%Y%m%dT%H%M%SZ")
    temp_context = None
    if args.synthetic_smoke and args.data_root == DEFAULT_DATA_ROOT:
        temp_context = tempfile.TemporaryDirectory(prefix="noisyoffice-external-synthetic-")
        args.data_root = Path(temp_context.name) / "data"
    try:
        if args.synthetic_smoke:
            write_synthetic_dataset(args.data_root)
            args.no_download = True
        run_root = (args.output_root / run_id).resolve()
        run_root.mkdir(parents=True, exist_ok=True)

        dataset = prepare_dataset(args.data_root, args.download_url, no_download=args.no_download)
        pairs = discover_pairs(dataset)
        print(f"NoisyOffice pairs: {len(pairs)}", flush=True)
        result, rows = run_external_cli(
            pairs,
            dataset=dataset,
            run_root=run_root,
            python_executable=args.python,
            workers=args.workers,
            rule_template=args.rule_template,
        )
        finished_at = datetime.now(timezone.utc)
        payload = build_payload(
            args=args,
            run_id=run_id,
            run_root=run_root,
            dataset=dataset,
            started_at=started_at,
            finished_at=finished_at,
            result=result,
            rows=rows,
        )
        write_outputs(payload, rows, run_root, None if args.no_doc_report else args.report_path)
        print(f"NoisyOffice external CLI test report: {run_root / 'noisyoffice_external_cli_test_report.md'}")
        if not args.no_doc_report:
            print(f"Docs report: {args.report_path.resolve()}")
        print(f"Result JSON: {run_root / 'noisyoffice_external_cli_test_results.json'}")
        print(f"Public OCR quality summary: {run_root / 'ocr_preprocessing_quality_summary.json'}")
        exit_passed = payload["summary"]["stable_cli_passed"]
        if args.enforce_ocr_quality_gate:
            exit_passed = exit_passed and bool(payload["summary"]["quality_gate"]["passed"])
        return 0 if exit_passed else 1
    finally:
        if temp_context is not None:
            temp_context.cleanup()


def prepare_dataset(data_root: Path, download_url: str, *, no_download: bool) -> Path:
    data_root.mkdir(parents=True, exist_ok=True)
    extracted = data_root / "extracted"
    dataset = extracted / "NoisyOffice" / "SimulatedNoisyOffice"
    source_dir = dataset / "simulated_noisy_images_grayscale"
    clean_dir = dataset / "clean_images_grayscale"
    if source_dir.is_dir() and clean_dir.is_dir():
        return dataset

    zip_path = data_root / "noisyoffice.zip"
    if not zip_path.is_file():
        if no_download:
            raise FileNotFoundError(f"NoisyOffice ZIP missing: {zip_path}")
        print(f"Downloading NoisyOffice from {download_url}", flush=True)
        urlretrieve(download_url, zip_path)
    with ZipFile(zip_path) as zf:
        bad = zf.testzip()
        if bad is not None:
            raise ValueError(f"NoisyOffice ZIP failed integrity check at {bad}")
    if not source_dir.is_dir() or not clean_dir.is_dir():
        print(f"Extracting NoisyOffice ZIP to {extracted}", flush=True)
        with ZipFile(zip_path) as zf:
            zf.extractall(extracted)
    if not source_dir.is_dir() or not clean_dir.is_dir():
        raise FileNotFoundError("NoisyOffice simulated noisy/clean directories were not found after extraction.")
    return dataset


def write_synthetic_dataset(data_root: Path) -> None:
    dataset = data_root / "extracted" / "NoisyOffice" / "SimulatedNoisyOffice"
    source_dir = dataset / "simulated_noisy_images_grayscale"
    clean_dir = dataset / "clean_images_grayscale"
    source_dir.mkdir(parents=True, exist_ok=True)
    clean_dir.mkdir(parents=True, exist_ok=True)
    for index, noise in enumerate(("Noise1", "Noise2"), start=1):
        clean_name = f"{index:04d}_Clean_001.png"
        noisy_name = f"{index:04d}_{noise}_001.png"
        clean = synthetic_clean_page(index)
        noisy = clean.copy()
        draw = ImageDraw.Draw(noisy)
        draw.point((24 + index * 8, 22), fill=0)
        draw.point((160, 120 - index * 6), fill=0)
        clean.save(clean_dir / clean_name, dpi=(300, 300))
        noisy.save(source_dir / noisy_name, dpi=(300, 300))
    (dataset / "SYNTHETIC_NOISYOFFICE_SMOKE.txt").write_text(
        "Synthetic NoisyOffice-shaped CI smoke data; not benchmark evidence.\n",
        encoding="utf-8",
    )


def synthetic_clean_page(index: int) -> Image.Image:
    image = Image.new("L", (220, 160), 255)
    draw = ImageDraw.Draw(image)
    draw.rectangle((24, 20, 196, 138), outline=32, width=2)
    for y in (42, 68, 94, 120):
        draw.rectangle((42, y, 174 - index * 7, y + 5), fill=24)
    return image


def discover_pairs(dataset: Path) -> list[ImagePair]:
    source_dir = dataset / "simulated_noisy_images_grayscale"
    clean_dir = dataset / "clean_images_grayscale"
    pairs: list[ImagePair] = []
    for source_path in sorted(source_dir.glob("*.png"), key=lambda path: path.name):
        clean_name = clean_name_for_noisy(source_path.name)
        clean_path = clean_dir / clean_name
        if not clean_path.is_file():
            raise FileNotFoundError(f"Missing clean GT for {source_path.name}: {clean_path}")
        pairs.append(
            ImagePair(
                source_path=source_path,
                clean_path=clean_path,
                relative_path=source_path.name,
                clean_id=clean_name,
                noise_type=noise_type_for_noisy(source_path.name),
            )
        )
    if not pairs:
        raise ValueError(f"No NoisyOffice source images found under {source_dir}")
    return pairs


def clean_name_for_noisy(name: str) -> str:
    stem = Path(name).stem
    parts = stem.split("_")
    if len(parts) != 3 or not parts[1].startswith("Noise"):
        raise ValueError(f"Unsupported NoisyOffice filename: {name}")
    return f"{parts[0]}_Clean_{parts[2]}.png"


def noise_type_for_noisy(name: str) -> str:
    noise_token = Path(name).stem.split("_")[1]
    return noise_token.replace("Noise", "") or "unknown"


def run_external_cli(
    pairs: list[ImagePair],
    *,
    dataset: Path,
    run_root: Path,
    python_executable: str,
    workers: int,
    rule_template: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    source_dir = dataset / "simulated_noisy_images_grayscale"
    derivative_dir = run_root / "derivatives"
    metadata_dir = run_root / "metadata"
    source_hashes_before = {pair.relative_path: sha256(pair.source_path) for pair in pairs}
    command = [
        python_executable,
        "-m",
        "archive_scan_qc.cli",
        "production-run",
        "--input",
        str(source_dir),
        "--derivatives-out",
        str(derivative_dir),
        "--metadata-out",
        str(metadata_dir),
        "--project",
        "noisyoffice-external-cli-test",
        "--batch",
        "simulated-noisy-grayscale",
        "--rule-template",
        rule_template,
        "--workers",
        str(workers),
        "--no-resume-processing",
    ]
    env = os.environ.copy()
    src_path = str(Path(__file__).resolve().parents[1] / "src")
    env["PYTHONPATH"] = src_path + os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else src_path
    started = time.perf_counter()
    completed = subprocess.run(command, cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True, text=True)
    wall_seconds = round(time.perf_counter() - started, 6)
    (run_root / "cli_command.json").write_text(
        json.dumps({"command": command, "returncode": completed.returncode}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (run_root / "cli_stdout.txt").write_text(completed.stdout, encoding="utf-8", errors="replace")
    (run_root / "cli_stderr.txt").write_text(completed.stderr, encoding="utf-8", errors="replace")
    source_hashes_after = {pair.relative_path: sha256(pair.source_path) for pair in pairs}
    source_modified = [name for name, digest in source_hashes_before.items() if source_hashes_after.get(name) != digest]

    summary = load_json(metadata_dir / "production_run_summary.json")
    progress = load_json(metadata_dir / "production_run_progress.json")
    manifest = load_json(derivative_dir / "processing_manifest.json")
    audit = load_json(derivative_dir / "processing_audit_summary.json")
    manifest_by_source = {
        record.get("source_relative_path"): record
        for record in manifest.get("files", [])
        if isinstance(record, dict) and isinstance(record.get("source_relative_path"), str)
    }
    rows = [
        evaluate_pair(pair, derivative_dir=derivative_dir, manifest_record=manifest_by_source.get(pair.relative_path))
        for pair in pairs
    ]
    dimensions = image_dimension_summary(pairs)
    stable_cli_passed = (
        summary.get("schema_version") == "scan-qc.production-run.v1"
        and progress.get("schema_version") == "scan-qc.production-run-progress.v1"
        and progress.get("state") in {"completed", "finished", "needs_review"}
        and int(summary.get("counts", {}).get("failed_files", -1)) == 0
        and not source_modified
        and len(rows) == len(pairs)
        and all(row.get("processed_output_found") for row in rows)
    )
    result = {
        "command_returncode": completed.returncode,
        "cli_wall_seconds": wall_seconds,
        "stable_cli_passed": stable_cli_passed,
        "source_modified_files": source_modified,
        "image_count": len(pairs),
        "total_megapixels": dimensions["total_megapixels"],
        "megapixels_per_second_wall": safe_div(dimensions["total_megapixels"], wall_seconds),
        "summary_status": summary.get("status"),
        "progress_state": progress.get("state"),
        "production_counts": summary.get("counts", {}),
        "production_performance": summary.get("performance", {}),
        "processing_audit_counts": audit.get("counts", {}),
        "operation_timings": manifest.get("summary", {}).get("performance", {}).get("operation_timings", {}),
        "rule_template": summary.get("rule_template"),
        "quality": {
            "source": aggregate_quality(rows, "source_"),
            "processed": aggregate_quality(rows, "processed_"),
            "delta": aggregate_quality_delta(aggregate_quality(rows, "source_"), aggregate_quality(rows, "processed_")),
            "by_noise_type": aggregate_by_noise_type(rows),
            "size_mismatch_files": sum(1 for row in rows if not row.get("processed_size_matches_clean")),
        },
        "dimensions": dimensions,
    }
    return result, rows


def evaluate_pair(pair: ImagePair, *, derivative_dir: Path, manifest_record: dict[str, Any] | None) -> dict[str, Any]:
    output_relative = manifest_record.get("output_relative_path") if isinstance(manifest_record, dict) else None
    processed_path = derivative_dir / output_relative if isinstance(output_relative, str) else None
    processed_found = bool(processed_path and processed_path.is_file())
    with Image.open(pair.source_path) as source_image, Image.open(pair.clean_path) as clean_image:
        source_gray = image_to_gray_array(source_image)
        clean_gray = image_to_gray_array(clean_image)
        source_size = source_image.size
        clean_size = clean_image.size
        source_eval = source_gray if source_gray.shape == clean_gray.shape else resize_array_to(source_gray, clean_gray.shape)
        source_metrics = image_quality_metrics(source_eval, clean_gray)
        source_stats = gray_stats(source_eval)

    processed_size: tuple[int, int] | None = None
    processed_size_matches_clean = False
    processed_metrics: dict[str, Any] = empty_metrics("processed_output_missing")
    processed_stats: dict[str, Any] = {}
    if processed_found and processed_path is not None:
        with Image.open(processed_path) as processed_image, Image.open(pair.clean_path) as clean_image:
            processed_gray = image_to_gray_array(processed_image)
            clean_gray = image_to_gray_array(clean_image)
            processed_size = processed_image.size
            processed_size_matches_clean = processed_gray.shape == clean_gray.shape
            processed_eval = processed_gray if processed_size_matches_clean else resize_array_to(processed_gray, clean_gray.shape)
            processed_metrics = image_quality_metrics(processed_eval, clean_gray)
            processed_stats = gray_stats(processed_eval)

    row = {
        "relative_path": pair.relative_path,
        "clean_id": pair.clean_id,
        "noise_type": pair.noise_type,
        "source_path": str(pair.source_path.resolve()),
        "clean_path": str(pair.clean_path.resolve()),
        "processed_path": str(processed_path.resolve()) if processed_path else None,
        "processed_output_found": processed_found,
        "source_width": source_size[0],
        "source_height": source_size[1],
        "clean_width": clean_size[0],
        "clean_height": clean_size[1],
        "processed_width": processed_size[0] if processed_size else None,
        "processed_height": processed_size[1] if processed_size else None,
        "source_size_matches_clean": source_size == clean_size,
        "processed_size_matches_clean": processed_size_matches_clean,
        "processing_status": manifest_record.get("status") if isinstance(manifest_record, dict) else None,
        "processing_operations": "|".join(manifest_record.get("operations", []))
        if isinstance(manifest_record, dict) and isinstance(manifest_record.get("operations"), list)
        else "",
        "processing_warnings": len(manifest_record.get("processing_warnings", []))
        if isinstance(manifest_record, dict) and isinstance(manifest_record.get("processing_warnings"), list)
        else 0,
        "processing_pixel_change_ratio": nested_number(manifest_record, "processing_audit", "pixel_change_ratio"),
        "processing_size_change_ratio": nested_number(manifest_record, "processing_audit", "size_change_ratio"),
        **prefix_dict("source_", source_metrics),
        **prefix_dict("processed_", processed_metrics),
        **prefix_dict("source_gray_", source_stats),
        **prefix_dict("processed_gray_", processed_stats),
    }
    for metric in ("psnr_db", "ssim", "mse", "mae", "dark_pixel_f1", "foreground_retention"):
        row[f"delta_{metric}"] = none_safe_subtract(row.get(f"processed_{metric}"), row.get(f"source_{metric}"))
    return row


def image_quality_metrics(candidate: "np.ndarray[Any, Any]", clean: "np.ndarray[Any, Any]") -> dict[str, Any]:
    candidate_f = candidate.astype(np.float64)
    clean_f = clean.astype(np.float64)
    diff = candidate_f - clean_f
    mse = float(np.mean(diff * diff))
    mae = float(np.mean(np.abs(diff)))
    psnr = 99.0 if mse <= 0 else 10.0 * math.log10((255.0 * 255.0) / mse)
    ssim = global_ssim(candidate_f, clean_f)
    source_dark = candidate <= 127
    clean_dark = clean <= 127
    tp = int(np.count_nonzero(source_dark & clean_dark))
    fp = int(np.count_nonzero(source_dark & ~clean_dark))
    fn = int(np.count_nonzero(~source_dark & clean_dark))
    precision = safe_div(tp, tp + fp)
    recall = safe_div(tp, tp + fn)
    return {
        "mse": rounded(mse, 6),
        "mae": rounded(mae, 6),
        "psnr_db": rounded(psnr, 4),
        "ssim": rounded(ssim, 6),
        "dark_pixel_precision": rounded(precision, 6),
        "dark_pixel_recall": rounded(recall, 6),
        "dark_pixel_f1": rounded(harmonic_mean(precision, recall), 6),
        "foreground_retention": rounded(recall, 6),
        "mean_delta": rounded(float(np.mean(candidate_f) - np.mean(clean_f)), 6),
    }


def global_ssim(left: "np.ndarray[Any, Any]", right: "np.ndarray[Any, Any]") -> float:
    c1 = (0.01 * 255.0) ** 2
    c2 = (0.03 * 255.0) ** 2
    mu_x = float(np.mean(left))
    mu_y = float(np.mean(right))
    sigma_x = float(np.var(left))
    sigma_y = float(np.var(right))
    sigma_xy = float(np.mean((left - mu_x) * (right - mu_y)))
    denominator = (mu_x * mu_x + mu_y * mu_y + c1) * (sigma_x + sigma_y + c2)
    if denominator == 0:
        return 1.0 if np.array_equal(left, right) else 0.0
    return ((2 * mu_x * mu_y + c1) * (2 * sigma_xy + c2)) / denominator


def aggregate_quality(rows: list[dict[str, Any]], prefix: str) -> dict[str, Any]:
    return {
        "image_count": len(rows),
        "psnr_db_macro": rounded(mean_or_none(numeric_values(rows, f"{prefix}psnr_db")), 4),
        "ssim_macro": rounded(mean_or_none(numeric_values(rows, f"{prefix}ssim")), 6),
        "mse_macro": rounded(mean_or_none(numeric_values(rows, f"{prefix}mse")), 6),
        "mae_macro": rounded(mean_or_none(numeric_values(rows, f"{prefix}mae")), 6),
        "dark_pixel_f1_macro": rounded(mean_or_none(numeric_values(rows, f"{prefix}dark_pixel_f1")), 6),
        "foreground_retention_macro": rounded(mean_or_none(numeric_values(rows, f"{prefix}foreground_retention")), 6),
        "mean_delta_macro": rounded(mean_or_none(numeric_values(rows, f"{prefix}mean_delta")), 6),
    }


def aggregate_quality_delta(source: dict[str, Any], processed: dict[str, Any]) -> dict[str, Any]:
    return {
        "psnr_db_macro": none_safe_subtract(processed.get("psnr_db_macro"), source.get("psnr_db_macro")),
        "ssim_macro": none_safe_subtract(processed.get("ssim_macro"), source.get("ssim_macro")),
        "mse_macro": none_safe_subtract(processed.get("mse_macro"), source.get("mse_macro")),
        "mae_macro": none_safe_subtract(processed.get("mae_macro"), source.get("mae_macro")),
        "dark_pixel_f1_macro": none_safe_subtract(
            processed.get("dark_pixel_f1_macro"), source.get("dark_pixel_f1_macro")
        ),
        "foreground_retention_macro": none_safe_subtract(
            processed.get("foreground_retention_macro"), source.get("foreground_retention_macro")
        ),
    }


def aggregate_by_noise_type(rows: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row["noise_type"]), []).append(row)
    result = {}
    for noise_type, items in sorted(grouped.items()):
        source = aggregate_quality(items, "source_")
        processed = aggregate_quality(items, "processed_")
        result[noise_type] = {
            "source": source,
            "processed": processed,
            "delta": aggregate_quality_delta(source, processed),
        }
    return result


def build_ocr_quality_gate(args: argparse.Namespace, quality: dict[str, Any]) -> dict[str, Any]:
    source = quality.get("source", {})
    processed = quality.get("processed", {})
    delta = quality.get("delta", {})
    by_noise_type = quality.get("by_noise_type", {})
    thresholds = {
        "min_psnr_delta_db": args.min_psnr_delta_db,
        "min_ssim_delta": args.min_ssim_delta,
        "min_mse_reduction_ratio": args.min_mse_reduction_ratio,
        "min_mae_reduction_ratio": args.min_mae_reduction_ratio,
        "min_dark_f1_delta": args.min_dark_f1_delta,
        "min_foreground_retention_delta": args.min_foreground_retention_delta,
        "min_positive_noise_groups": args.min_positive_noise_groups,
        "max_negative_noise_groups": 0,
    }
    mse_reduction = reduction_ratio(source.get("mse_macro"), processed.get("mse_macro"))
    mae_reduction = reduction_ratio(source.get("mae_macro"), processed.get("mae_macro"))
    noise_group_checks = noise_group_quality_checks(by_noise_type)
    checks = {
        "psnr_delta_db": gate_check(delta.get("psnr_db_macro"), args.min_psnr_delta_db, direction="gte"),
        "ssim_delta": gate_check(delta.get("ssim_macro"), args.min_ssim_delta, direction="gte"),
        "mse_reduction_ratio": gate_check(mse_reduction, args.min_mse_reduction_ratio, direction="gte"),
        "mae_reduction_ratio": gate_check(mae_reduction, args.min_mae_reduction_ratio, direction="gte"),
        "dark_f1_delta": gate_check(delta.get("dark_pixel_f1_macro"), args.min_dark_f1_delta, direction="gte"),
        "foreground_retention_delta": gate_check(
            delta.get("foreground_retention_macro"), args.min_foreground_retention_delta, direction="gte"
        ),
        "positive_noise_groups": {
            "actual": noise_group_checks["positive_count"],
            "threshold": args.min_positive_noise_groups,
            "passed": noise_group_checks["positive_count"] >= args.min_positive_noise_groups,
        },
        "negative_noise_groups": {
            "actual": noise_group_checks["negative_count"],
            "threshold": 0,
            "passed": noise_group_checks["negative_count"] == 0,
            "groups": noise_group_checks["negative_groups"],
        },
    }
    failed_codes = [key for key, value in checks.items() if isinstance(value, dict) and not value.get("passed")]
    return {
        "enabled": bool(args.enforce_ocr_quality_gate),
        "profile": args.rule_template,
        "thresholds": thresholds,
        "checks": checks,
        "noise_groups": noise_group_checks["groups"],
        "failed_codes": failed_codes,
        "passed": not failed_codes,
    }


def noise_group_quality_checks(by_noise_type: dict[str, Any]) -> dict[str, Any]:
    groups: dict[str, Any] = {}
    positive_count = 0
    negative_groups: list[str] = []
    for noise_type, payload in sorted(by_noise_type.items()):
        delta = payload.get("delta", {}) if isinstance(payload, dict) else {}
        psnr_delta = delta.get("psnr_db_macro")
        ssim_delta = delta.get("ssim_macro")
        mse_reduction = reduction_ratio(
            payload.get("source", {}).get("mse_macro") if isinstance(payload, dict) else None,
            payload.get("processed", {}).get("mse_macro") if isinstance(payload, dict) else None,
        )
        mae_reduction = reduction_ratio(
            payload.get("source", {}).get("mae_macro") if isinstance(payload, dict) else None,
            payload.get("processed", {}).get("mae_macro") if isinstance(payload, dict) else None,
        )
        positive = positive_number(psnr_delta) or positive_number(ssim_delta)
        negative = negative_number(psnr_delta) or negative_number(ssim_delta)
        if positive:
            positive_count += 1
        if negative:
            negative_groups.append(str(noise_type))
        groups[str(noise_type)] = {
            "image_count": nested_number(payload.get("source", {}), "image_count") if isinstance(payload, dict) else None,
            "psnr_delta_db": psnr_delta,
            "ssim_delta": ssim_delta,
            "mse_reduction_ratio": mse_reduction,
            "mae_reduction_ratio": mae_reduction,
            "positive": positive,
            "negative": negative,
        }
    return {
        "groups": groups,
        "positive_count": positive_count,
        "negative_count": len(negative_groups),
        "negative_groups": negative_groups,
    }


def gate_check(actual: Any, threshold: float, *, direction: str) -> dict[str, Any]:
    numeric_actual = float(actual) if isinstance(actual, int | float) and math.isfinite(float(actual)) else None
    if numeric_actual is None:
        passed = False
    elif direction == "gte":
        passed = numeric_actual >= threshold
    else:
        raise ValueError(f"unsupported gate direction: {direction}")
    return {"actual": rounded(numeric_actual), "threshold": threshold, "passed": passed}


def reduction_ratio(source: Any, processed: Any) -> float | None:
    if not isinstance(source, int | float) or not isinstance(processed, int | float):
        return None
    if not math.isfinite(float(source)) or not math.isfinite(float(processed)) or float(source) <= 0:
        return None
    return rounded((float(source) - float(processed)) / float(source), 6)


def positive_number(value: Any) -> bool:
    return isinstance(value, int | float) and math.isfinite(float(value)) and float(value) > 0


def negative_number(value: Any) -> bool:
    return isinstance(value, int | float) and math.isfinite(float(value)) and float(value) < 0


def build_payload(
    *,
    args: argparse.Namespace,
    run_id: str,
    run_root: Path,
    dataset: Path,
    started_at: datetime,
    finished_at: datetime,
    result: dict[str, Any],
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    elapsed_seconds = round((finished_at - started_at).total_seconds(), 6)
    quality_gate = build_ocr_quality_gate(args, result["quality"])
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "generated_at": finished_at.isoformat(),
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "elapsed_seconds": elapsed_seconds,
        "run_root": str(run_root),
        "environment": {
            "python_executable": args.python,
            "python_version": platform.python_version(),
            "platform": platform.platform(),
            "workers": args.workers,
            "rule_template": args.rule_template,
            "data_root": str(args.data_root.resolve()),
            "dataset_root": str(dataset.resolve()),
            "download_url": args.download_url,
            "numpy_version": np.__version__,
            "pillow_version": Image.__version__ if hasattr(Image, "__version__") else None,
        },
        "summary": {
            "stable_cli_passed": result["stable_cli_passed"],
            "image_count": result["image_count"],
            "total_megapixels": result["total_megapixels"],
            "elapsed_seconds": elapsed_seconds,
            "images_per_minute_end_to_end": rounded((result["image_count"] / elapsed_seconds) * 60, 2)
            if elapsed_seconds
            else None,
            "megapixels_per_second_end_to_end": rounded(result["total_megapixels"] / elapsed_seconds, 4)
            if elapsed_seconds
            else None,
            "source_modified_files": len(result["source_modified_files"]),
            "processed_output_missing_files": sum(1 for row in rows if not row.get("processed_output_found")),
            "processed_size_mismatch_files": result["quality"]["size_mismatch_files"],
            "quality": result["quality"],
            "quality_gate": quality_gate,
        },
        "dataset": result,
        "worst_cases": worst_cases(rows),
    }


def write_outputs(payload: dict[str, Any], rows: list[dict[str, Any]], run_root: Path, doc_report_path: Path | None) -> None:
    json_path = run_root / "noisyoffice_external_cli_test_results.json"
    csv_path = run_root / "noisyoffice_external_cli_image_metrics.csv"
    report_path = run_root / "noisyoffice_external_cli_test_report.md"
    public_summary_path = run_root / "ocr_preprocessing_quality_summary.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    public_summary_path.write_text(
        json.dumps(build_public_ocr_quality_summary(payload), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    write_csv(csv_path, rows)
    report = render_markdown_report(payload, json_path=json_path, image_csv_path=csv_path)
    report_path.write_text(report, encoding="utf-8")
    if doc_report_path is not None:
        doc_report_path.parent.mkdir(parents=True, exist_ok=True)
        doc_report_path.write_text(report, encoding="utf-8")


def build_public_ocr_quality_summary(payload: dict[str, Any]) -> dict[str, Any]:
    summary = payload["summary"]
    quality = summary["quality"]
    return {
        "schema_version": OCR_QUALITY_SUMMARY_SCHEMA_VERSION,
        "generated_at": payload["generated_at"],
        "rule_template": payload["environment"]["rule_template"],
        "stable_cli_passed": summary["stable_cli_passed"],
        "image_count": summary["image_count"],
        "quality_gate": summary["quality_gate"],
        "quality": public_quality_block(quality),
        "privacy": {
            "public_safe": True,
            "contains_paths": False,
            "contains_file_names": False,
            "contains_hashes": False,
            "contains_ocr_text": False,
            "contains_image_content": False,
            "contains_row_level_records": False,
            "source": "aggregate metrics only",
        },
    }


def public_quality_block(quality: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": metric_subset(quality.get("source", {})),
        "processed": metric_subset(quality.get("processed", {})),
        "delta": metric_subset(quality.get("delta", {})),
        "size_mismatch_files": quality.get("size_mismatch_files"),
        "by_noise_type": {
            str(noise_type): {
                "source": metric_subset(payload.get("source", {}) if isinstance(payload, dict) else {}),
                "processed": metric_subset(payload.get("processed", {}) if isinstance(payload, dict) else {}),
                "delta": metric_subset(payload.get("delta", {}) if isinstance(payload, dict) else {}),
                "mse_reduction_ratio": reduction_ratio(
                    payload.get("source", {}).get("mse_macro") if isinstance(payload, dict) else None,
                    payload.get("processed", {}).get("mse_macro") if isinstance(payload, dict) else None,
                ),
                "mae_reduction_ratio": reduction_ratio(
                    payload.get("source", {}).get("mae_macro") if isinstance(payload, dict) else None,
                    payload.get("processed", {}).get("mae_macro") if isinstance(payload, dict) else None,
                ),
            }
            for noise_type, payload in sorted(quality.get("by_noise_type", {}).items())
        },
    }


def metric_subset(payload: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "image_count",
        "psnr_db_macro",
        "ssim_macro",
        "mse_macro",
        "mae_macro",
        "dark_pixel_f1_macro",
        "foreground_retention_macro",
        "mean_delta_macro",
    )
    return {key: payload.get(key) for key in keys if key in payload}


def render_markdown_report(payload: dict[str, Any], *, json_path: Path, image_csv_path: Path) -> str:
    summary = payload["summary"]
    quality = summary["quality"]
    production = payload["dataset"]
    perf = production.get("production_performance", {})
    scan_perf = perf.get("scan", {}) if isinstance(perf, dict) else {}
    proc_perf = perf.get("processing", {}) if isinstance(perf, dict) else {}
    stable = "通过" if summary["stable_cli_passed"] else "未通过"
    lines = [
        "# NoisyOffice 外部 CLI 批量质检修图测试报告",
        "",
        "## 结论摘要",
        "",
        f"- 外部 CLI 稳定性验收：{stable}。",
        f"- 测试范围：UCI NoisyOffice simulated noisy grayscale，共 {summary['image_count']} 张图，合计 {summary['total_megapixels']} MP。",
        f"- 端到端耗时：{summary['elapsed_seconds']} 秒；吞吐：{summary['images_per_minute_end_to_end']} 张/分钟，{summary['megapixels_per_second_end_to_end']} MP/s。",
        f"- CLI wall time：{production['cli_wall_seconds']} 秒；扫描：{fmt(scan_perf.get('elapsed_seconds'))} 秒；处理：{fmt(proc_perf.get('elapsed_seconds'))} 秒。",
        f"- 原图被修改文件数：{summary['source_modified_files']}；处理输出缺失文件数：{summary['processed_output_missing_files']}；处理失败文件数：{production.get('production_counts', {}).get('failed_files')}。",
        f"- 处理后尺寸与 clean GT 不一致文件数：{summary['processed_size_mismatch_files']}。",
        "",
        "## 测试方法",
        "",
        "- 数据来源：UCI NoisyOffice，使用 simulated noisy grayscale 作为输入，clean grayscale 作为 GT。",
        "- 外部调用方式：脚本以子进程执行 `python -m archive_scan_qc.cli production-run`，不直接调用后端内部函数。",
        f"- 规则模板：`{payload['environment']['rule_template']}`。",
        "- 质量指标：PSNR、SSIM、MSE、MAE、暗像素 F1、前景保留率、亮度均值偏差。",
        "- PSNR/SSIM/暗像素 F1/前景保留率越高越好；MSE/MAE 越低越好。",
        "",
        "## 整体质量指标",
        "",
        "| 对象 | PSNR dB | SSIM | MSE | MAE | 暗像素 F1 | 前景保留率 |",
        "|---|---:|---:|---:|---:|---:|---:|",
        quality_row("Noisy 输入基线", quality["source"]),
        quality_row("处理后", quality["processed"]),
        delta_row("处理后-输入", quality["delta"]),
        "",
        "## 噪声类型分组",
        "",
        "| 噪声类型 | 图像数 | 输入 PSNR | 处理后 PSNR | PSNR 变化 | 输入 SSIM | 处理后 SSIM | SSIM 变化 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for noise_type, item in quality["by_noise_type"].items():
        lines.append(
            "| {noise} | {count} | {src_psnr} | {proc_psnr} | {delta_psnr} | {src_ssim} | {proc_ssim} | {delta_ssim} |".format(
                noise=noise_type,
                count=item["source"]["image_count"],
                src_psnr=fmt(item["source"].get("psnr_db_macro")),
                proc_psnr=fmt(item["processed"].get("psnr_db_macro")),
                delta_psnr=fmt(item["delta"].get("psnr_db_macro"), signed=True),
                src_ssim=fmt(item["source"].get("ssim_macro")),
                proc_ssim=fmt(item["processed"].get("ssim_macro")),
                delta_ssim=fmt(item["delta"].get("ssim_macro"), signed=True),
            )
        )
    lines.extend(
        [
            "",
            "## 外部 CLI 稳定性检查",
            "",
            "| return code | summary status | progress state | processed | skipped | failed | source modified | stable pass |",
            "|---:|---|---|---:|---:|---:|---:|---|",
            "| {returncode} | {status} | {state} | {processed} | {skipped} | {failed} | {modified} | {passed} |".format(
                returncode=production["command_returncode"],
                status=production["summary_status"],
                state=production["progress_state"],
                processed=production.get("production_counts", {}).get("processed_files"),
                skipped=production.get("production_counts", {}).get("skipped_files"),
                failed=production.get("production_counts", {}).get("failed_files"),
                modified=len(production["source_modified_files"]),
                passed="yes" if production["stable_cli_passed"] else "no",
            ),
            "",
            "## 处理操作统计",
            "",
            "| warnings | guardrail failed | despeckled | tone normalized | edge shadow | faded text | sharpen text |",
            "|---:|---:|---:|---:|---:|---:|---:|",
            "| {warnings} | {guardrail} | {despeckled} | {tone} | {edge} | {faded} | {sharpen} |".format(
                warnings=production.get("processing_audit_counts", {}).get("processing_warning_files"),
                guardrail=production.get("processing_audit_counts", {}).get("guardrail_failed_files"),
                despeckled=production.get("processing_audit_counts", {}).get("despeckled_files"),
                tone=production.get("processing_audit_counts", {}).get("tone_normalized_files"),
                edge=production.get("processing_audit_counts", {}).get("edge_shadow_lightened_files"),
                faded=production.get("processing_audit_counts", {}).get("faded_text_enhanced_files"),
                sharpen=production.get("processing_audit_counts", {}).get("text_edges_sharpened_files"),
            ),
            "",
            "## 质量变化最差样本",
            "",
            "| 文件 | 噪声类型 | 输入 PSNR | 处理后 PSNR | PSNR 变化 | 输入 SSIM | 处理后 SSIM | SSIM 变化 |",
            "|---|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in payload["worst_cases"]["largest_psnr_drop"][:10]:
        lines.append(
            "| {path} | {noise} | {src_psnr} | {proc_psnr} | {delta_psnr} | {src_ssim} | {proc_ssim} | {delta_ssim} |".format(
                path=row["relative_path"],
                noise=row["noise_type"],
                src_psnr=fmt(row.get("source_psnr_db")),
                proc_psnr=fmt(row.get("processed_psnr_db")),
                delta_psnr=fmt(row.get("delta_psnr_db"), signed=True),
                src_ssim=fmt(row.get("source_ssim")),
                proc_ssim=fmt(row.get("processed_ssim")),
                delta_ssim=fmt(row.get("delta_ssim"), signed=True),
            )
        )
    lines.extend(
        [
            "",
            "## 重要限制",
            "",
            "- 当前测试评估的是修图输出与 clean grayscale GT 的接近程度，不评估官方二值化任务。",
            "- NoisyOffice 中部分噪声类型可能是大面积纹理/污渍，当前保守 guardrail 可能选择少处理或回退，以避免破坏文字和档案原貌。",
            "- 如果目标是最大化 NoisyOffice PSNR/SSIM，需要新增专门的强去噪/背景重建模板，而不是直接使用当前生产保守模板。",
            "",
            "## 产物",
            "",
            f"- JSON 结果：`{json_path.resolve()}`",
            f"- 图像级 CSV：`{image_csv_path.resolve()}`",
            f"- 运行根目录：`{Path(payload['run_root']).resolve()}`",
            "",
        ]
    )
    lines.extend(render_quality_gate_lines(summary["quality_gate"]))
    return "\n".join(lines)


def render_quality_gate_lines(gate: dict[str, Any]) -> list[str]:
    gate_status = "passed" if gate["passed"] else "failed"
    lines = [
        "",
        "## OCR preprocessing quality gate",
        "",
        f"- Gate status: {gate_status}; enforced: {'yes' if gate['enabled'] else 'no'}.",
        f"- Failed checks: {', '.join(gate['failed_codes']) if gate['failed_codes'] else 'none'}.",
        "",
        "| check | actual | threshold | passed |",
        "|---|---:|---:|---|",
    ]
    for key, check in gate["checks"].items():
        actual = check.get("actual") if isinstance(check, dict) else None
        threshold = check.get("threshold") if isinstance(check, dict) else None
        passed = check.get("passed") if isinstance(check, dict) else False
        lines.append(f"| {key} | {fmt(actual)} | {fmt(threshold)} | {'yes' if passed else 'no'} |")
    return lines


def quality_row(label: str, metrics: dict[str, Any]) -> str:
    return (
        f"| {label} | {fmt(metrics.get('psnr_db_macro'))} | {fmt(metrics.get('ssim_macro'))} | "
        f"{fmt(metrics.get('mse_macro'))} | {fmt(metrics.get('mae_macro'))} | "
        f"{fmt(metrics.get('dark_pixel_f1_macro'))} | {fmt(metrics.get('foreground_retention_macro'))} |"
    )


def delta_row(label: str, metrics: dict[str, Any]) -> str:
    return (
        f"| {label} | {fmt(metrics.get('psnr_db_macro'), signed=True)} | {fmt(metrics.get('ssim_macro'), signed=True)} | "
        f"{fmt(metrics.get('mse_macro'), signed=True)} | {fmt(metrics.get('mae_macro'), signed=True)} | "
        f"{fmt(metrics.get('dark_pixel_f1_macro'), signed=True)} | {fmt(metrics.get('foreground_retention_macro'), signed=True)} |"
    )


def worst_cases(rows: list[dict[str, Any]]) -> dict[str, Any]:
    evaluated = [row for row in rows if row.get("processed_output_found")]
    return {
        "largest_psnr_drop": sanitize_rows(
            sorted(
                evaluated,
                key=lambda row: row.get("delta_psnr_db") if isinstance(row.get("delta_psnr_db"), int | float) else 0,
            )[:15]
        ),
        "largest_ssim_drop": sanitize_rows(
            sorted(
                evaluated,
                key=lambda row: row.get("delta_ssim") if isinstance(row.get("delta_ssim"), int | float) else 0,
            )[:15]
        ),
    }


def sanitize_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    keys = {
        "relative_path",
        "noise_type",
        "source_psnr_db",
        "processed_psnr_db",
        "delta_psnr_db",
        "source_ssim",
        "processed_ssim",
        "delta_ssim",
        "source_mae",
        "processed_mae",
        "delta_mae",
        "source_dark_pixel_f1",
        "processed_dark_pixel_f1",
        "delta_dark_pixel_f1",
    }
    return [{key: row.get(key) for key in keys if key in row} for row in rows]


def image_to_gray_array(image: Image.Image) -> "np.ndarray[Any, Any]":
    return np.asarray(image.convert("L"), dtype=np.uint8)


def resize_array_to(array: "np.ndarray[Any, Any]", target_shape: tuple[int, int]) -> "np.ndarray[Any, Any]":
    target_height, target_width = target_shape
    image = Image.fromarray(array, mode="L")
    resized = image.resize((target_width, target_height), Image.Resampling.BILINEAR)
    return np.asarray(resized, dtype=np.uint8)


def gray_stats(gray: "np.ndarray[Any, Any]") -> dict[str, Any]:
    return {
        "mean": rounded(float(np.mean(gray)), 4),
        "stddev": rounded(float(np.std(gray)), 4),
        "p05": rounded(float(np.percentile(gray, 5)), 4),
        "p50": rounded(float(np.percentile(gray, 50)), 4),
        "p95": rounded(float(np.percentile(gray, 95)), 4),
    }


def image_dimension_summary(pairs: list[ImagePair]) -> dict[str, Any]:
    pixels = 0
    widths: list[int] = []
    heights: list[int] = []
    for pair in pairs:
        with Image.open(pair.source_path) as image:
            width, height = image.size
        widths.append(width)
        heights.append(height)
        pixels += width * height
    return {
        "min_width": min(widths),
        "max_width": max(widths),
        "min_height": min(heights),
        "max_height": max(heights),
        "total_pixels": pixels,
        "total_megapixels": rounded(pixels / 1_000_000, 4),
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = sorted({key for row in rows for key in row.keys()})
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def nested_number(payload: dict[str, Any] | None, *keys: str) -> float | int | None:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current if isinstance(current, int | float) else None


def prefix_dict(prefix: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {f"{prefix}{key}": value for key, value in payload.items()}


def empty_metrics(reason: str) -> dict[str, Any]:
    return {
        "reason": reason,
        "mse": None,
        "mae": None,
        "psnr_db": None,
        "ssim": None,
        "dark_pixel_precision": None,
        "dark_pixel_recall": None,
        "dark_pixel_f1": None,
        "foreground_retention": None,
        "mean_delta": None,
    }


def numeric_values(rows: list[dict[str, Any]], key: str) -> list[float]:
    return [float(row[key]) for row in rows if isinstance(row.get(key), int | float) and math.isfinite(float(row[key]))]


def mean_or_none(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def none_safe_subtract(left: Any, right: Any) -> float | None:
    if isinstance(left, int | float) and isinstance(right, int | float):
        return rounded(float(left) - float(right), 6)
    return None


def safe_div(numerator: int | float, denominator: int | float) -> float | None:
    if denominator == 0:
        return None
    return float(numerator) / float(denominator)


def harmonic_mean(left: float | None, right: float | None) -> float | None:
    if left is None or right is None or left + right == 0:
        return None
    return 2 * left * right / (left + right)


def rounded(value: float | None, digits: int = 6) -> float | None:
    if value is None or not math.isfinite(float(value)):
        return None
    return round(float(value), digits)


def fmt(value: Any, *, signed: bool = False) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, int | float):
        return f"{value:+.6f}" if signed else f"{value:.6f}"
    return str(value)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
