"""Run an external CLI validation on the local DIBCO/H-DIBCO datasets.

The script intentionally calls ``archive_scan_qc`` through a child Python
process. It does not import the production runner for execution, so the run
matches an external CLI caller's view of the current package.
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

from PIL import Image, ImageDraw

try:
    import numpy as np
except ImportError as exc:  # pragma: no cover - exercised by operator environment.
    raise SystemExit(
        "DIBCO external CLI test requires numpy for full-image quality metrics. "
        "Install with: python -m pip install numpy"
    ) from exc


SCHEMA_VERSION = "scan-qc.dibco-external-cli-test.v1"
DEFAULT_DATA_ROOT = Path(r"D:\data-opt\DIBCO-H-DIBCO")
DEFAULT_OUTPUT_ROOT = Path("generated") / "dibco_external_cli_test"
DEFAULT_DOC_REPORT = Path("docs") / "dibco-external-cli-test-report.md"
SUPPORTED_EXTENSIONS = {".bmp", ".dib", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}


@dataclass(frozen=True)
class DatasetSpec:
    dataset_id: str
    label: str
    source_dir: Path
    gt_dir: Path
    gt_suffix: str = ""


@dataclass(frozen=True)
class ImagePair:
    dataset_id: str
    source_path: Path
    gt_path: Path
    relative_path: str


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", default=DEFAULT_DATA_ROOT, type=Path, help="Root containing extracted DIBCO data.")
    parser.add_argument("--output-root", default=DEFAULT_OUTPUT_ROOT, type=Path, help="Directory for generated test artifacts.")
    parser.add_argument(
        "--report-path",
        default=DEFAULT_DOC_REPORT,
        type=Path,
        help="Markdown report path. Defaults to docs/dibco-external-cli-test-report.md.",
    )
    parser.add_argument("--python", default=sys.executable, help="Python executable used for the external CLI process.")
    parser.add_argument("--workers", default=min(4, os.cpu_count() or 1), type=_positive_int, help="CLI worker count.")
    parser.add_argument(
        "--rule-template",
        default="text-clean-print",
        choices=("dat-31-2017-standard", "text-clean-print", "high-fidelity-original"),
        help="Rule template used for production-run.",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="Optional stable run id. Defaults to a UTC timestamp.",
    )
    parser.add_argument(
        "--no-doc-report",
        action="store_true",
        help="Skip writing the docs report and only write generated artifacts.",
    )
    parser.add_argument(
        "--synthetic-smoke",
        action="store_true",
        help="Create a tiny DIBCO-shaped synthetic dataset under --data-root for CI smoke validation.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    started_at = datetime.now(timezone.utc)
    run_id = args.run_id or started_at.strftime("%Y%m%dT%H%M%SZ")
    temp_context = None
    if args.synthetic_smoke and args.data_root == DEFAULT_DATA_ROOT:
        temp_context = tempfile.TemporaryDirectory(prefix="dibco-external-synthetic-")
        args.data_root = Path(temp_context.name) / "data"
    try:
        if args.synthetic_smoke:
            write_synthetic_dataset(args.data_root)
        run_root = (args.output_root / run_id).resolve()
        run_root.mkdir(parents=True, exist_ok=True)

        specs = dataset_specs(args.data_root)
        pairs_by_dataset = {spec.dataset_id: discover_pairs(spec) for spec in specs}
        dataset_results: list[dict[str, Any]] = []
        image_rows: list[dict[str, Any]] = []

        for spec in specs:
            print(f"Running external CLI dataset: {spec.dataset_id}", flush=True)
            result, rows = run_dataset(
                spec,
                pairs_by_dataset[spec.dataset_id],
                run_root=run_root,
                python_executable=args.python,
                workers=args.workers,
                rule_template=args.rule_template,
            )
            dataset_results.append(result)
            image_rows.extend(rows)
            print(
                f"Completed {spec.dataset_id}: stable={result['stable_cli_passed']} "
                f"images={result['image_count']} wall={result['cli_wall_seconds']}s",
                flush=True,
            )

        finished_at = datetime.now(timezone.utc)
        payload = build_payload(
            args=args,
            run_id=run_id,
            run_root=run_root,
            started_at=started_at,
            finished_at=finished_at,
            dataset_results=dataset_results,
            image_rows=image_rows,
        )
        write_outputs(payload, image_rows, run_root, None if args.no_doc_report else args.report_path)
        print(f"DIBCO external CLI test report: {run_root / 'dibco_external_cli_test_report.md'}")
        if not args.no_doc_report:
            print(f"Docs report: {args.report_path.resolve()}")
        print(f"Result JSON: {run_root / 'dibco_external_cli_test_results.json'}")
        return 0 if payload["summary"]["stable_cli_passed"] else 1
    finally:
        if temp_context is not None:
            temp_context.cleanup()


def dataset_specs(data_root: Path) -> list[DatasetSpec]:
    extracted = data_root / "extracted"
    return [
        DatasetSpec(
            dataset_id="dibco2019_trackA",
            label="DIBCO 2019 Track A",
            source_dir=extracted / "dibco2019_trackA" / "Dataset",
            gt_dir=extracted / "dibco2019_trackA" / "GT",
        ),
        DatasetSpec(
            dataset_id="dibco2019_trackB",
            label="DIBCO 2019 Track B",
            source_dir=extracted / "dibco2019_trackB" / "Dataset",
            gt_dir=extracted / "dibco2019_trackB" / "GT",
        ),
        DatasetSpec(
            dataset_id="hdibco2018",
            label="H-DIBCO 2018",
            source_dir=extracted / "hdibco2018" / "dataset",
            gt_dir=extracted / "hdibco2018" / "gt",
            gt_suffix="_gt",
        ),
    ]


def write_synthetic_dataset(data_root: Path) -> None:
    extracted = data_root / "extracted"
    specs = dataset_specs(data_root)
    for spec in specs:
        spec.source_dir.mkdir(parents=True, exist_ok=True)
        spec.gt_dir.mkdir(parents=True, exist_ok=True)
    for spec in specs:
        for index in range(1, 3):
            source_name = f"synthetic_{index:03d}.png"
            gt_name = f"synthetic_{index:03d}{spec.gt_suffix}.png"
            image = synthetic_document_page(index)
            image.save(spec.source_dir / source_name, dpi=(300, 300))
            image.save(spec.gt_dir / gt_name, dpi=(300, 300))
    marker = extracted / "SYNTHETIC_DIBCO_SMOKE.txt"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("Synthetic DIBCO-shaped CI smoke data; not benchmark evidence.\n", encoding="utf-8")


def synthetic_document_page(index: int) -> Image.Image:
    image = Image.new("RGB", (220, 160), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((24, 20, 196, 138), outline=(30, 30, 30), width=2)
    for y in (42, 68, 94, 120):
        draw.rectangle((42, y, 176 - index * 8, y + 5), fill=(20, 20, 20))
    if index % 2 == 0:
        draw.rectangle((0, 0, 8, 159), fill=(230, 230, 230))
    return image


def discover_pairs(spec: DatasetSpec) -> list[ImagePair]:
    if not spec.source_dir.is_dir():
        raise FileNotFoundError(f"Dataset source directory does not exist: {spec.source_dir}")
    if not spec.gt_dir.is_dir():
        raise FileNotFoundError(f"Dataset GT directory does not exist: {spec.gt_dir}")
    pairs: list[ImagePair] = []
    for source in sorted((path for path in spec.source_dir.iterdir() if path.suffix.lower() in SUPPORTED_EXTENSIONS), key=_path_sort_key):
        gt_name = f"{source.stem}{spec.gt_suffix}{source.suffix}"
        gt_path = spec.gt_dir / gt_name
        if not gt_path.is_file():
            raise FileNotFoundError(f"Missing GT image for {source}: {gt_path}")
        pairs.append(
            ImagePair(
                dataset_id=spec.dataset_id,
                source_path=source,
                gt_path=gt_path,
                relative_path=source.name,
            )
        )
    if not pairs:
        raise ValueError(f"No supported image pairs found in {spec.source_dir}")
    return pairs


def run_dataset(
    spec: DatasetSpec,
    pairs: list[ImagePair],
    *,
    run_root: Path,
    python_executable: str,
    workers: int,
    rule_template: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    dataset_root = run_root / spec.dataset_id
    derivative_dir = dataset_root / "derivatives"
    metadata_dir = dataset_root / "metadata"
    source_hashes_before = {pair.relative_path: sha256(pair.source_path) for pair in pairs}
    command = [
        python_executable,
        "-m",
        "archive_scan_qc.cli",
        "production-run",
        "--input",
        str(spec.source_dir),
        "--derivatives-out",
        str(derivative_dir),
        "--metadata-out",
        str(metadata_dir),
        "--project",
        "dibco-external-cli-test",
        "--batch",
        spec.dataset_id,
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
    dataset_root.mkdir(parents=True, exist_ok=True)
    (dataset_root / "cli_command.json").write_text(
        json.dumps({"command": command, "returncode": completed.returncode}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (dataset_root / "cli_stdout.txt").write_text(completed.stdout, encoding="utf-8", errors="replace")
    (dataset_root / "cli_stderr.txt").write_text(completed.stderr, encoding="utf-8", errors="replace")

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
    aggregate = aggregate_quality(rows)
    source_aggregate = aggregate_quality(rows, prefix="source_")
    processed_aggregate = aggregate_quality(rows, prefix="processed_")
    dimensions = image_dimension_summary(pairs)
    stable_cli_passed = (
        summary.get("schema_version") == "scan-qc.production-run.v1"
        and progress.get("schema_version") == "scan-qc.production-run-progress.v1"
        and progress.get("state") in {"completed", "needs_review", "finished"}
        and int(summary.get("counts", {}).get("failed_files", -1)) == 0
        and not source_modified
        and len(rows) == len(pairs)
        and all(row.get("processed_output_found") for row in rows)
    )
    result = {
        "dataset_id": spec.dataset_id,
        "label": spec.label,
        "source_dir": str(spec.source_dir.resolve()),
        "gt_dir": str(spec.gt_dir.resolve()),
        "metadata_dir": str(metadata_dir.resolve()),
        "derivative_dir": str(derivative_dir.resolve()),
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
            "source": source_aggregate,
            "processed": processed_aggregate,
            "delta": aggregate_quality_delta(source_aggregate, processed_aggregate),
            "size_mismatch_files": sum(1 for row in rows if not row.get("processed_size_matches_gt")),
        },
        "dimensions": dimensions,
    }
    return result, rows


def evaluate_pair(pair: ImagePair, *, derivative_dir: Path, manifest_record: dict[str, Any] | None) -> dict[str, Any]:
    output_relative = manifest_record.get("output_relative_path") if isinstance(manifest_record, dict) else None
    processed_path = derivative_dir / output_relative if isinstance(output_relative, str) else None
    processed_found = bool(processed_path and processed_path.is_file())
    with Image.open(pair.source_path) as source_image, Image.open(pair.gt_path) as gt_image:
        source_gray = image_to_gray_array(source_image)
        gt_gray = image_to_gray_array(gt_image)
        source_size = source_image.size
        gt_size = gt_image.size
        gt_context = gt_quality_context(gt_gray)
        if source_gray.shape != gt_gray.shape:
            source_eval = resize_array_to(source_gray, gt_gray.shape)
        else:
            source_eval = source_gray
        source_metrics = binary_quality_metrics(source_eval, gt_context)
        source_stats = gray_stats(source_eval)

    processed_metrics: dict[str, Any] = empty_metric_payload("processed_output_missing")
    processed_stats: dict[str, Any] = {}
    processed_size: tuple[int, int] | None = None
    processed_size_matches_gt = False
    if processed_found and processed_path is not None:
        with Image.open(processed_path) as processed_image, Image.open(pair.gt_path) as gt_image:
            processed_gray = image_to_gray_array(processed_image)
            gt_gray = image_to_gray_array(gt_image)
            processed_size = processed_image.size
            processed_size_matches_gt = processed_gray.shape == gt_gray.shape
            processed_eval = processed_gray if processed_size_matches_gt else resize_array_to(processed_gray, gt_gray.shape)
            processed_metrics = binary_quality_metrics(processed_eval, gt_context)
            processed_stats = gray_stats(processed_eval)

    row = {
        "dataset_id": pair.dataset_id,
        "relative_path": pair.relative_path,
        "source_path": str(pair.source_path.resolve()),
        "gt_path": str(pair.gt_path.resolve()),
        "processed_path": str(processed_path.resolve()) if processed_path else None,
        "processed_output_found": processed_found,
        "source_width": source_size[0],
        "source_height": source_size[1],
        "gt_width": gt_size[0],
        "gt_height": gt_size[1],
        "processed_width": processed_size[0] if processed_size else None,
        "processed_height": processed_size[1] if processed_size else None,
        "source_size_matches_gt": source_size == gt_size,
        "processed_size_matches_gt": processed_size_matches_gt,
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
    row["delta_f1"] = none_safe_subtract(row.get("processed_f1"), row.get("source_f1"))
    row["delta_pseudo_f1"] = none_safe_subtract(row.get("processed_pseudo_f1"), row.get("source_pseudo_f1"))
    row["delta_psnr_db"] = none_safe_subtract(row.get("processed_psnr_db"), row.get("source_psnr_db"))
    row["delta_drd"] = none_safe_subtract(row.get("processed_drd"), row.get("source_drd"))
    row["delta_iou"] = none_safe_subtract(row.get("processed_iou"), row.get("source_iou"))
    return row


def gt_quality_context(gt_gray: "np.ndarray[Any, Any]") -> dict[str, Any]:
    gt_fg = gt_gray <= 127
    nubn = non_uniform_block_count(gt_fg)
    return {
        "gt_fg": gt_fg,
        "gt_skeleton": skeletonize(gt_fg),
        "non_uniform_blocks": nubn,
        "drd_cost": drd_cost_map(gt_fg) if nubn > 0 else None,
    }


def binary_quality_metrics(candidate_gray: "np.ndarray[Any, Any]", gt_context: dict[str, Any]) -> dict[str, Any]:
    threshold = otsu_threshold(candidate_gray)
    pred_fg = candidate_gray <= threshold
    gt_fg = gt_context["gt_fg"]
    gt_skeleton = gt_context["gt_skeleton"]
    tp = int(np.count_nonzero(pred_fg & gt_fg))
    fp = int(np.count_nonzero(pred_fg & ~gt_fg))
    fn = int(np.count_nonzero(~pred_fg & gt_fg))
    tn = int(np.count_nonzero(~pred_fg & ~gt_fg))
    total = int(pred_fg.size)
    precision = safe_div(tp, tp + fp)
    recall = safe_div(tp, tp + fn)
    f1 = harmonic_mean(precision, recall)
    specificity = safe_div(tn, tn + fp)
    iou = safe_div(tp, tp + fp + fn)
    mismatch = fp + fn
    mismatch_rate = safe_div(mismatch, total)
    pseudo_recall = safe_div(int(np.count_nonzero(pred_fg & gt_skeleton)), int(np.count_nonzero(gt_skeleton)))
    pseudo_f1 = harmonic_mean(precision, pseudo_recall)
    return {
        "threshold": int(threshold),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "pixel_count": total,
        "foreground_ratio": round(float(np.count_nonzero(pred_fg)) / total, 8) if total else None,
        "gt_foreground_ratio": round(float(np.count_nonzero(gt_fg)) / total, 8) if total else None,
        "precision": rounded(precision),
        "recall": rounded(recall),
        "f1": rounded(f1),
        "pseudo_recall": rounded(pseudo_recall),
        "pseudo_f1": rounded(pseudo_f1),
        "iou": rounded(iou),
        "accuracy": rounded(safe_div(tp + tn, total)),
        "specificity": rounded(specificity),
        "false_positive_rate": rounded(safe_div(fp, fp + tn)),
        "false_negative_rate": rounded(safe_div(fn, fn + tp)),
        "mismatch_rate": rounded(mismatch_rate),
        "psnr_db": rounded(psnr_from_mismatch_rate(mismatch_rate), digits=4),
        "drd": rounded(drd(pred_fg, gt_fg, gt_context), digits=6),
        "non_uniform_gt_blocks": int(gt_context["non_uniform_blocks"]),
    }


def skeletonize(mask: "np.ndarray[Any, Any]") -> "np.ndarray[Any, Any]":
    image = mask.astype(bool).copy()
    if image.size == 0:
        return image
    changed = True
    iterations = 0
    while changed and iterations < 80:
        changed = False
        iterations += 1
        for step in (0, 1):
            padded = np.pad(image, 1, mode="constant", constant_values=False)
            p2 = padded[:-2, 1:-1]
            p3 = padded[:-2, 2:]
            p4 = padded[1:-1, 2:]
            p5 = padded[2:, 2:]
            p6 = padded[2:, 1:-1]
            p7 = padded[2:, :-2]
            p8 = padded[1:-1, :-2]
            p9 = padded[:-2, :-2]
            neighbors = (
                p2.astype(np.uint8)
                + p3.astype(np.uint8)
                + p4.astype(np.uint8)
                + p5.astype(np.uint8)
                + p6.astype(np.uint8)
                + p7.astype(np.uint8)
                + p8.astype(np.uint8)
                + p9.astype(np.uint8)
            )
            transitions = (
                (~p2 & p3).astype(np.uint8)
                + (~p3 & p4).astype(np.uint8)
                + (~p4 & p5).astype(np.uint8)
                + (~p5 & p6).astype(np.uint8)
                + (~p6 & p7).astype(np.uint8)
                + (~p7 & p8).astype(np.uint8)
                + (~p8 & p9).astype(np.uint8)
                + (~p9 & p2).astype(np.uint8)
            )
            if step == 0:
                condition = ~(p2 & p4 & p6) & ~(p4 & p6 & p8)
            else:
                condition = ~(p2 & p4 & p8) & ~(p2 & p6 & p8)
            remove = image & (neighbors >= 2) & (neighbors <= 6) & (transitions == 1) & condition
            if np.any(remove):
                image[remove] = False
                changed = True
    return image


def drd(pred_fg: "np.ndarray[Any, Any]", gt_fg: "np.ndarray[Any, Any]", gt_context: dict[str, Any]) -> float | None:
    nubn = int(gt_context["non_uniform_blocks"])
    if nubn <= 0:
        return None
    mismatches = pred_fg != gt_fg
    if not np.any(mismatches):
        return 0.0
    cost = gt_context.get("drd_cost")
    if cost is None:
        return None
    return float(np.sum(cost[mismatches]) / nubn)


def drd_cost_map(gt_fg: "np.ndarray[Any, Any]") -> "np.ndarray[Any, Any]":
    gt = gt_fg.astype(np.float32)
    padded = np.pad(gt, 2, mode="edge")
    weights = drd_weights()
    cost = np.zeros(gt.shape, dtype=np.float32)
    height, width = gt.shape
    for dy in range(-2, 3):
        for dx in range(-2, 3):
            weight = weights[dy + 2, dx + 2]
            if weight == 0:
                continue
            neighbor = padded[2 + dy : 2 + dy + height, 2 + dx : 2 + dx + width]
            cost += np.abs(neighbor - gt) * weight
    return cost


def drd_weights() -> "np.ndarray[Any, Any]":
    weights = np.zeros((5, 5), dtype=np.float32)
    for y in range(5):
        for x in range(5):
            dy = y - 2
            dx = x - 2
            if dx == 0 and dy == 0:
                continue
            weights[y, x] = 1.0 / math.sqrt(float(dx * dx + dy * dy))
    total = float(weights.sum())
    return weights / total if total else weights


def non_uniform_block_count(gt_fg: "np.ndarray[Any, Any]", block_size: int = 8) -> int:
    height, width = gt_fg.shape
    count = 0
    for top in range(0, height, block_size):
        for left in range(0, width, block_size):
            block = gt_fg[top : top + block_size, left : left + block_size]
            if block.size and np.any(block) and not np.all(block):
                count += 1
    return count


def otsu_threshold(gray: "np.ndarray[Any, Any]") -> int:
    hist = np.bincount(gray.reshape(-1), minlength=256).astype(np.float64)
    total = gray.size
    if total <= 0:
        return 127
    levels = np.arange(256, dtype=np.float64)
    sum_total = float(np.dot(levels, hist))
    weight_background = 0.0
    sum_background = 0.0
    best_threshold = 127
    best_variance = -1.0
    for threshold in range(256):
        weight_background += hist[threshold]
        if weight_background <= 0:
            continue
        weight_foreground = total - weight_background
        if weight_foreground <= 0:
            break
        sum_background += threshold * hist[threshold]
        mean_background = sum_background / weight_background
        mean_foreground = (sum_total - sum_background) / weight_foreground
        variance = weight_background * weight_foreground * (mean_background - mean_foreground) ** 2
        if variance > best_variance:
            best_variance = variance
            best_threshold = threshold
    return int(best_threshold)


def image_to_gray_array(image: Image.Image) -> "np.ndarray[Any, Any]":
    return np.asarray(image.convert("L"), dtype=np.uint8)


def resize_array_to(array: "np.ndarray[Any, Any]", target_shape: tuple[int, int]) -> "np.ndarray[Any, Any]":
    target_height, target_width = target_shape
    image = Image.fromarray(array, mode="L")
    resized = image.resize((target_width, target_height), Image.Resampling.BILINEAR)
    return np.asarray(resized, dtype=np.uint8)


def gray_stats(gray: "np.ndarray[Any, Any]") -> dict[str, Any]:
    return {
        "mean": rounded(float(np.mean(gray)), digits=4),
        "stddev": rounded(float(np.std(gray)), digits=4),
        "p05": rounded(float(np.percentile(gray, 5)), digits=4),
        "p50": rounded(float(np.percentile(gray, 50)), digits=4),
        "p95": rounded(float(np.percentile(gray, 95)), digits=4),
    }


def aggregate_quality(rows: list[dict[str, Any]], prefix: str = "") -> dict[str, Any]:
    if prefix:
        values = lambda name: numeric_values(rows, f"{prefix}{name}")
        sums = lambda name: sum(int(row.get(f"{prefix}{name}") or 0) for row in rows)
    else:
        values = lambda name: numeric_values(rows, name)
        sums = lambda name: sum(int(row.get(name) or 0) for row in rows)
    tp = sums("tp")
    fp = sums("fp")
    fn = sums("fn")
    tn = sums("tn")
    total = tp + fp + fn + tn
    precision = safe_div(tp, tp + fp)
    recall = safe_div(tp, tp + fn)
    skeleton_recall_values = values("pseudo_recall")
    pseudo_recall = statistics.fmean(skeleton_recall_values) if skeleton_recall_values else None
    return {
        "image_count": len(rows),
        "pixel_count": total,
        "precision_micro": rounded(precision),
        "recall_micro": rounded(recall),
        "f1_micro": rounded(harmonic_mean(precision, recall)),
        "pseudo_recall_macro": rounded(pseudo_recall),
        "f1_macro": rounded(mean_or_none(values("f1"))),
        "pseudo_f1_macro": rounded(mean_or_none(values("pseudo_f1"))),
        "iou_macro": rounded(mean_or_none(values("iou"))),
        "accuracy_macro": rounded(mean_or_none(values("accuracy"))),
        "psnr_db_macro": rounded(mean_or_none(values("psnr_db")), digits=4),
        "drd_macro": rounded(mean_or_none(values("drd")), digits=6),
        "mismatch_rate_macro": rounded(mean_or_none(values("mismatch_rate"))),
        "foreground_ratio_macro": rounded(mean_or_none(values("foreground_ratio"))),
        "gt_foreground_ratio_macro": rounded(mean_or_none(values("gt_foreground_ratio"))),
    }


def aggregate_quality_delta(source: dict[str, Any], processed: dict[str, Any]) -> dict[str, Any]:
    return {
        "f1_macro": none_safe_subtract(processed.get("f1_macro"), source.get("f1_macro")),
        "pseudo_f1_macro": none_safe_subtract(processed.get("pseudo_f1_macro"), source.get("pseudo_f1_macro")),
        "psnr_db_macro": none_safe_subtract(processed.get("psnr_db_macro"), source.get("psnr_db_macro")),
        "drd_macro": none_safe_subtract(processed.get("drd_macro"), source.get("drd_macro")),
        "mismatch_rate_macro": none_safe_subtract(processed.get("mismatch_rate_macro"), source.get("mismatch_rate_macro")),
    }


def build_payload(
    *,
    args: argparse.Namespace,
    run_id: str,
    run_root: Path,
    started_at: datetime,
    finished_at: datetime,
    dataset_results: list[dict[str, Any]],
    image_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    elapsed_seconds = round((finished_at - started_at).total_seconds(), 6)
    source_overall = aggregate_quality(image_rows, prefix="source_")
    processed_overall = aggregate_quality(image_rows, prefix="processed_")
    stable_passed = all(result["stable_cli_passed"] for result in dataset_results)
    total_images = sum(result["image_count"] for result in dataset_results)
    total_megapixels = round(sum(float(result["total_megapixels"]) for result in dataset_results), 4)
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
            "processor": platform.processor(),
            "workers": args.workers,
            "rule_template": args.rule_template,
            "data_root": str(args.data_root.resolve()),
            "numpy_version": np.__version__,
            "pillow_version": Image.__version__ if hasattr(Image, "__version__") else None,
        },
        "summary": {
            "stable_cli_passed": stable_passed,
            "dataset_count": len(dataset_results),
            "image_count": total_images,
            "total_megapixels": total_megapixels,
            "elapsed_seconds": elapsed_seconds,
            "images_per_minute_end_to_end": rounded((total_images / elapsed_seconds) * 60 if elapsed_seconds else None, digits=2),
            "megapixels_per_second_end_to_end": rounded(total_megapixels / elapsed_seconds if elapsed_seconds else None, digits=4),
            "source_modified_files": sum(len(result["source_modified_files"]) for result in dataset_results),
            "processed_output_missing_files": sum(1 for row in image_rows if not row.get("processed_output_found")),
            "processed_size_mismatch_files": sum(1 for row in image_rows if not row.get("processed_size_matches_gt")),
            "quality": {
                "source": source_overall,
                "processed": processed_overall,
                "delta": aggregate_quality_delta(source_overall, processed_overall),
            },
        },
        "datasets": dataset_results,
        "worst_cases": worst_cases(image_rows),
    }


def write_outputs(
    payload: dict[str, Any],
    image_rows: list[dict[str, Any]],
    run_root: Path,
    doc_report_path: Path | None,
) -> None:
    json_path = run_root / "dibco_external_cli_test_results.json"
    csv_path = run_root / "dibco_external_cli_image_metrics.csv"
    dataset_csv_path = run_root / "dibco_external_cli_dataset_summary.csv"
    report_path = run_root / "dibco_external_cli_test_report.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_csv(csv_path, image_rows)
    write_csv(dataset_csv_path, dataset_summary_rows(payload["datasets"]))
    report = render_markdown_report(payload, json_path=json_path, image_csv_path=csv_path, dataset_csv_path=dataset_csv_path)
    report_path.write_text(report, encoding="utf-8")
    if doc_report_path is not None:
        doc_report_path.parent.mkdir(parents=True, exist_ok=True)
        doc_report_path.write_text(report, encoding="utf-8")


def render_markdown_report(payload: dict[str, Any], *, json_path: Path, image_csv_path: Path, dataset_csv_path: Path) -> str:
    summary = payload["summary"]
    quality = summary["quality"]
    stable = "通过" if summary["stable_cli_passed"] else "未通过"
    lines = [
        "# DIBCO/H-DIBCO 外部 CLI 批量质检修图测试报告",
        "",
        "## 结论摘要",
        "",
        f"- 外部 CLI 稳定性验收：{stable}。",
        f"- 测试范围：3 个数据集，{summary['image_count']} 张图，合计 {summary['total_megapixels']} MP。",
        f"- 端到端耗时：{summary['elapsed_seconds']} 秒；吞吐：{summary['images_per_minute_end_to_end']} 张/分钟，{summary['megapixels_per_second_end_to_end']} MP/s。",
        f"- 原图被修改文件数：{summary['source_modified_files']}；处理输出缺失文件数：{summary['processed_output_missing_files']}。",
        f"- 处理后尺寸与 GT 不一致文件数：{summary['processed_size_mismatch_files']}。尺寸不一致的图像在质量评估时被重采样到 GT 尺寸，相关指标需结合该限制解读。",
        "",
        "## 测试方法",
        "",
        "- 外部调用方式：脚本以子进程执行 `python -m archive_scan_qc.cli production-run`，不直接调用后端内部函数。",
        f"- 规则模板：`{payload['environment']['rule_template']}`。",
        "- 质量对比：对原图和处理后图分别用 Otsu 阈值二值化，再与 DIBCO/H-DIBCO GT 二值图比较。",
        "- 主要质量指标：Precision、Recall、F1、Pseudo-F1、IoU、Accuracy、PSNR、DRD、Mismatch rate。",
        "- DRD 越低越好；F1、Pseudo-F1、IoU、Accuracy、PSNR 越高越好。",
        "",
        "## 整体质量指标",
        "",
        "| 对象 | F1 macro | Pseudo-F1 macro | IoU macro | PSNR dB macro | DRD macro | Mismatch rate macro |",
        "|---|---:|---:|---:|---:|---:|---:|",
        quality_row("原图基线", quality["source"]),
        quality_row("处理后", quality["processed"]),
        delta_row("处理后-原图", quality["delta"]),
        "",
        "## 数据集级性能与质量",
        "",
        "| 数据集 | 图像数 | CLI wall s | 扫描 s | 处理 s | wall MP/s | 输出缺失 | 尺寸不一致 | F1 原图 | F1 处理后 | F1 变化 | DRD 原图 | DRD 处理后 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for dataset in payload["datasets"]:
        perf = dataset.get("production_performance", {})
        scan_perf = perf.get("scan", {}) if isinstance(perf, dict) else {}
        proc_perf = perf.get("processing", {}) if isinstance(perf, dict) else {}
        q_source = dataset["quality"]["source"]
        q_processed = dataset["quality"]["processed"]
        q_delta = dataset["quality"]["delta"]
        lines.append(
            "| {label} | {count} | {wall} | {scan} | {process} | {mp_s} | {missing} | {mismatch} | {src_f1} | {proc_f1} | {delta_f1} | {src_drd} | {proc_drd} |".format(
                label=dataset["label"],
                count=dataset["image_count"],
                wall=fmt(dataset["cli_wall_seconds"]),
                scan=fmt(scan_perf.get("elapsed_seconds")),
                process=fmt(proc_perf.get("elapsed_seconds")),
                mp_s=fmt(dataset["megapixels_per_second_wall"]),
                missing=sum(1 for item in payload.get("worst_cases", {}).get("missing_outputs", []) if item.get("dataset_id") == dataset["dataset_id"]),
                mismatch=dataset["quality"]["size_mismatch_files"],
                src_f1=fmt(q_source.get("f1_macro")),
                proc_f1=fmt(q_processed.get("f1_macro")),
                delta_f1=fmt(q_delta.get("f1_macro"), signed=True),
                src_drd=fmt(q_source.get("drd_macro")),
                proc_drd=fmt(q_processed.get("drd_macro")),
            )
        )
    lines.extend(
        [
            "",
            "## 外部 CLI 稳定性检查",
            "",
            "| 数据集 | return code | summary status | progress state | failed files | source modified | stable pass |",
            "|---|---:|---|---|---:|---:|---|",
        ]
    )
    for dataset in payload["datasets"]:
        counts = dataset.get("production_counts", {})
        lines.append(
            "| {label} | {returncode} | {status} | {state} | {failed} | {modified} | {passed} |".format(
                label=dataset["label"],
                returncode=dataset["command_returncode"],
                status=dataset["summary_status"],
                state=dataset["progress_state"],
                failed=counts.get("failed_files"),
                modified=len(dataset["source_modified_files"]),
                passed="yes" if dataset["stable_cli_passed"] else "no",
            )
        )
    lines.extend(
        [
            "",
            "## 处理操作统计",
            "",
            "| 数据集 | processed | warnings | guardrail failed | despeckled | tone normalized | edge shadow | faded text | sharpen text |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for dataset in payload["datasets"]:
        counts = dataset.get("production_counts", {})
        audit = dataset.get("processing_audit_counts", {})
        lines.append(
            "| {label} | {processed} | {warnings} | {guardrail} | {despeckled} | {tone} | {edge} | {faded} | {sharpen} |".format(
                label=dataset["label"],
                processed=counts.get("processed_files"),
                warnings=audit.get("processing_warning_files"),
                guardrail=audit.get("guardrail_failed_files"),
                despeckled=audit.get("despeckled_files"),
                tone=audit.get("tone_normalized_files"),
                edge=audit.get("edge_shadow_lightened_files"),
                faded=audit.get("faded_text_enhanced_files"),
                sharpen=audit.get("text_edges_sharpened_files"),
            )
        )
    lines.extend(
        [
            "",
            "## 质量变化最差样本",
            "",
            "| 数据集 | 文件 | F1 原图 | F1 处理后 | F1 变化 | DRD 原图 | DRD 处理后 | 尺寸一致 |",
            "|---|---|---:|---:|---:|---:|---:|---|",
        ]
    )
    for row in payload["worst_cases"]["largest_f1_drop"]:
        lines.append(
            "| {dataset} | {path} | {src_f1} | {proc_f1} | {delta} | {src_drd} | {proc_drd} | {size_match} |".format(
                dataset=row["dataset_id"],
                path=row["relative_path"],
                src_f1=fmt(row.get("source_f1")),
                proc_f1=fmt(row.get("processed_f1")),
                delta=fmt(row.get("delta_f1"), signed=True),
                src_drd=fmt(row.get("source_drd")),
                proc_drd=fmt(row.get("processed_drd")),
                size_match="yes" if row.get("processed_size_matches_gt") else "no",
            )
        )
    lines.extend(
        [
            "",
            "## 重要限制",
            "",
            "- 当前程序是质检修图服务，不是 DIBCO 专用二值化模型；报告中的 DIBCO 指标来自同一 Otsu 后处理阈值下的可比评估。",
            "- `text-clean-print` 会启用裁切、去斑、色调/阴影/文字增强等规则；如果几何处理改变尺寸，DIBCO GT 对齐指标会受到额外影响。",
            "- DIBCO 2019 Track A/B 的 BMP 元数据 DPI 为约 72，低于模板面向生产扫描件的 DPI 要求，因此 CLI return code 可能因复核项为非 0；稳定性以终态产物和失败数为主。",
            "",
            "## 产物",
            "",
            f"- JSON 结果：`{json_path.resolve()}`",
            f"- 图像级 CSV：`{image_csv_path.resolve()}`",
            f"- 数据集级 CSV：`{dataset_csv_path.resolve()}`",
            f"- 运行根目录：`{Path(payload['run_root']).resolve()}`",
            "",
        ]
    )
    return "\n".join(lines)


def quality_row(label: str, metrics: dict[str, Any]) -> str:
    return (
        f"| {label} | {fmt(metrics.get('f1_macro'))} | {fmt(metrics.get('pseudo_f1_macro'))} | "
        f"{fmt(metrics.get('iou_macro'))} | {fmt(metrics.get('psnr_db_macro'))} | "
        f"{fmt(metrics.get('drd_macro'))} | {fmt(metrics.get('mismatch_rate_macro'))} |"
    )


def delta_row(label: str, metrics: dict[str, Any]) -> str:
    return (
        f"| {label} | {fmt(metrics.get('f1_macro'), signed=True)} | {fmt(metrics.get('pseudo_f1_macro'), signed=True)} | "
        f"n/a | {fmt(metrics.get('psnr_db_macro'), signed=True)} | {fmt(metrics.get('drd_macro'), signed=True)} | "
        f"{fmt(metrics.get('mismatch_rate_macro'), signed=True)} |"
    )


def worst_cases(rows: list[dict[str, Any]]) -> dict[str, Any]:
    evaluated = [row for row in rows if row.get("processed_output_found")]
    largest_f1_drop = sorted(
        evaluated,
        key=lambda row: row.get("delta_f1") if isinstance(row.get("delta_f1"), int | float) else 0,
    )[:10]
    largest_drd_increase = sorted(
        evaluated,
        key=lambda row: row.get("delta_drd") if isinstance(row.get("delta_drd"), int | float) else 0,
        reverse=True,
    )[:10]
    return {
        "largest_f1_drop": sanitize_rows(largest_f1_drop),
        "largest_drd_increase": sanitize_rows(largest_drd_increase),
        "missing_outputs": sanitize_rows([row for row in rows if not row.get("processed_output_found")]),
    }


def sanitize_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    public_keys = {
        "dataset_id",
        "relative_path",
        "processed_output_found",
        "processed_size_matches_gt",
        "source_f1",
        "processed_f1",
        "delta_f1",
        "source_pseudo_f1",
        "processed_pseudo_f1",
        "delta_pseudo_f1",
        "source_drd",
        "processed_drd",
        "delta_drd",
        "source_psnr_db",
        "processed_psnr_db",
        "delta_psnr_db",
    }
    return [{key: row.get(key) for key in public_keys if key in row} for row in rows]


def dataset_summary_rows(datasets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for dataset in datasets:
        perf = dataset.get("production_performance", {})
        scan_perf = perf.get("scan", {}) if isinstance(perf, dict) else {}
        proc_perf = perf.get("processing", {}) if isinstance(perf, dict) else {}
        rows.append(
            {
                "dataset_id": dataset["dataset_id"],
                "label": dataset["label"],
                "stable_cli_passed": dataset["stable_cli_passed"],
                "command_returncode": dataset["command_returncode"],
                "summary_status": dataset["summary_status"],
                "progress_state": dataset["progress_state"],
                "image_count": dataset["image_count"],
                "total_megapixels": dataset["total_megapixels"],
                "cli_wall_seconds": dataset["cli_wall_seconds"],
                "scan_elapsed_seconds": scan_perf.get("elapsed_seconds"),
                "processing_elapsed_seconds": proc_perf.get("elapsed_seconds"),
                "megapixels_per_second_wall": dataset["megapixels_per_second_wall"],
                "source_f1_macro": dataset["quality"]["source"].get("f1_macro"),
                "processed_f1_macro": dataset["quality"]["processed"].get("f1_macro"),
                "delta_f1_macro": dataset["quality"]["delta"].get("f1_macro"),
                "source_drd_macro": dataset["quality"]["source"].get("drd_macro"),
                "processed_drd_macro": dataset["quality"]["processed"].get("drd_macro"),
                "delta_drd_macro": dataset["quality"]["delta"].get("drd_macro"),
                "processed_size_mismatch_files": dataset["quality"]["size_mismatch_files"],
            }
        )
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = sorted({key for row in rows for key in row.keys()})
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def image_dimension_summary(pairs: list[ImagePair]) -> dict[str, Any]:
    widths: list[int] = []
    heights: list[int] = []
    pixels = 0
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
        "total_megapixels": round(pixels / 1_000_000, 4),
    }


def load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
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


def empty_metric_payload(reason: str) -> dict[str, Any]:
    return {
        "reason": reason,
        "threshold": None,
        "tp": 0,
        "fp": 0,
        "fn": 0,
        "tn": 0,
        "pixel_count": 0,
        "precision": None,
        "recall": None,
        "f1": None,
        "pseudo_recall": None,
        "pseudo_f1": None,
        "iou": None,
        "accuracy": None,
        "psnr_db": None,
        "drd": None,
        "mismatch_rate": None,
    }


def numeric_values(rows: list[dict[str, Any]], key: str) -> list[float]:
    return [float(row[key]) for row in rows if isinstance(row.get(key), int | float) and math.isfinite(float(row[key]))]


def mean_or_none(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def none_safe_subtract(left: Any, right: Any) -> float | None:
    if isinstance(left, int | float) and isinstance(right, int | float):
        return rounded(float(left) - float(right))
    return None


def safe_div(numerator: int | float, denominator: int | float) -> float | None:
    if denominator == 0:
        return None
    return float(numerator) / float(denominator)


def harmonic_mean(left: float | None, right: float | None) -> float | None:
    if left is None or right is None or left + right == 0:
        return None
    return 2 * left * right / (left + right)


def psnr_from_mismatch_rate(rate: float | None) -> float | None:
    if rate is None:
        return None
    if rate <= 0:
        return 99.0
    return -10.0 * math.log10(rate)


def rounded(value: float | None, digits: int = 6) -> float | None:
    if value is None:
        return None
    if not math.isfinite(float(value)):
        return None
    return round(float(value), digits)


def fmt(value: Any, *, signed: bool = False) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, int | float):
        if signed:
            return f"{value:+.6f}"
        return f"{value:.6f}"
    return str(value)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _path_sort_key(path: Path) -> tuple[int, int | str]:
    try:
        return (0, int(path.stem))
    except ValueError:
        return (1, path.name)


if __name__ == "__main__":
    raise SystemExit(main())
