"""Local derivative-image processing for scanned-image batches.

The processing layer never modifies source images. It writes derivative files
and a manifest that links each output back to the original scan record.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError


@dataclass(frozen=True)
class ProcessingOptions:
    auto_crop: bool = False
    deskew: bool = False
    deskew_max_degrees: float = 5.0
    deskew_min_confidence: float = 0.08


@dataclass(frozen=True)
class SkewDetection:
    angle_degrees: float | None
    confidence: float
    reason: str


def process_images(
    report: dict[str, Any],
    input_dir: Path,
    process_dir: Path,
    options: ProcessingOptions | None = None,
) -> dict[str, Any]:
    started_at = datetime.now(timezone.utc)
    start_seconds = time.perf_counter()
    options = options or ProcessingOptions()
    input_dir = input_dir.resolve()
    process_dir = process_dir.resolve()
    image_root = process_dir / "images"
    records = [_process_record(item, input_dir, image_root, options) for item in report["files"]]
    processed_files = sum(1 for item in records if item["status"] == "processed")
    skipped_files = sum(1 for item in records if item["status"] == "skipped")
    failed_files = sum(1 for item in records if item["status"] == "failed")
    finished_at = datetime.now(timezone.utc)
    performance = _performance_summary(
        started_at,
        finished_at,
        time.perf_counter() - start_seconds,
        total_files=len(records),
        processed_files=processed_files,
        skipped_files=skipped_files,
        failed_files=failed_files,
    )
    manifest = {
        "schema_version": "scan-qc.processing.v1",
        "generated_at": finished_at.isoformat(),
        "project": report.get("project", {}),
        "source_report_schema_version": report.get("schema_version"),
        "process_dir": str(process_dir),
        "image_root": str(image_root),
        "summary": {
            "total_files": len(records),
            "processed_files": processed_files,
            "skipped_files": skipped_files,
            "failed_files": failed_files,
            "performance": performance,
        },
        "performance": performance,
        "operations": [
            "exif_transpose",
            "convert_non_l_or_rgb_to_rgb",
            "skew_detect_projection",
            "deskew_conservative" if options.deskew else "deskew_disabled",
            "auto_crop_conservative" if options.auto_crop else "auto_crop_disabled",
            "autocontrast_cutoff_0_5",
            "preserve_source_relative_path",
        ],
        "files": records,
    }
    process_dir.mkdir(parents=True, exist_ok=True)
    (process_dir / "processing_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def _performance_summary(
    started_at: datetime,
    finished_at: datetime,
    elapsed_seconds: float,
    *,
    total_files: int,
    processed_files: int,
    skipped_files: int,
    failed_files: int,
) -> dict[str, Any]:
    elapsed_seconds = max(0.0, round(elapsed_seconds, 6))
    return {
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "elapsed_seconds": elapsed_seconds,
        "total_files": total_files,
        "processed_files": processed_files,
        "skipped_files": skipped_files,
        "failed_files": failed_files,
        "processed_files_per_minute": _files_per_minute(processed_files, elapsed_seconds),
        "total_files_per_minute": _files_per_minute(total_files, elapsed_seconds),
    }


def _files_per_minute(file_count: int, elapsed_seconds: float) -> float:
    if elapsed_seconds <= 0:
        return 0.0
    return round((file_count / elapsed_seconds) * 60, 2)


def _process_record(
    item: dict[str, Any],
    input_dir: Path,
    image_root: Path,
    options: ProcessingOptions,
) -> dict[str, Any]:
    relative_path = item["relative_path"]
    base = {
        "source_relative_path": relative_path,
        "source_sha256": item.get("sha256"),
        "output_relative_path": None,
        "output_sha256": None,
        "original_size": None,
        "output_size": None,
        "pre_deskew_size": None,
        "post_deskew_size": None,
        "skew_angle_degrees": None,
        "skew_confidence": 0.0,
        "deskewed": False,
        "deskew_reason": None,
        "crop_bbox": None,
        "cropped": False,
        "status": "skipped",
        "operations": [],
        "error": None,
        "failure_reason": None,
    }
    if not item.get("openable"):
        base["failure_reason"] = "source image is not openable"
        base["error"] = base["failure_reason"]
        return base

    source = input_dir / relative_path
    target = image_root / relative_path
    try:
        with Image.open(source) as image:
            processed, operations, process_info = _process_image(image, options)
            target.parent.mkdir(parents=True, exist_ok=True)
            _save_image(processed, target, image)
        base.update(
            {
                "output_relative_path": target.relative_to(image_root.parent).as_posix(),
                "output_sha256": _sha256(target),
                "original_size": process_info["original_size"],
                "output_size": process_info["output_size"],
                "pre_deskew_size": process_info["pre_deskew_size"],
                "post_deskew_size": process_info["post_deskew_size"],
                "skew_angle_degrees": process_info["skew_angle_degrees"],
                "skew_confidence": process_info["skew_confidence"],
                "deskewed": process_info["deskewed"],
                "deskew_reason": process_info["deskew_reason"],
                "crop_bbox": process_info["crop_bbox"],
                "cropped": process_info["cropped"],
                "status": "processed",
                "operations": operations,
            }
        )
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        base["status"] = "failed"
        base["error"] = str(exc)
        base["failure_reason"] = str(exc)
    return base


def _process_image(image: Image.Image, options: ProcessingOptions) -> tuple[Image.Image, list[str], dict[str, Any]]:
    operations: list[str] = []
    processed = ImageOps.exif_transpose(image)
    operations.append("exif_transpose")
    original_size = list(processed.size)

    if processed.mode not in {"L", "RGB"}:
        processed = processed.convert("RGB")
        operations.append("convert_to_rgb")

    pre_deskew_size = list(processed.size)
    post_deskew_size = list(processed.size)
    skew = _detect_skew(processed)
    operations.append("skew_detect_projection")
    deskewed = False
    deskew_reason = skew.reason
    if not options.deskew:
        operations.append("deskew_disabled")
        deskew_reason = "deskew disabled"
    elif skew.angle_degrees is None:
        operations.append("deskew_noop")
    elif skew.confidence < options.deskew_min_confidence:
        operations.append("deskew_noop")
        deskew_reason = "low confidence"
    elif abs(skew.angle_degrees) > options.deskew_max_degrees:
        operations.append("deskew_noop")
        deskew_reason = "angle exceeds conservative threshold"
    elif abs(skew.angle_degrees) < 0.2:
        operations.append("deskew_noop")
        deskew_reason = "angle below correction threshold"
    else:
        processed = _rotate_for_deskew(processed, -skew.angle_degrees)
        operations.append("deskew_conservative")
        post_deskew_size = list(processed.size)
        deskewed = True
        deskew_reason = "deskew applied"

    crop_bbox: tuple[int, int, int, int] | None = None
    if options.auto_crop:
        crop_bbox = _detect_conservative_crop_bbox(processed)
        if crop_bbox:
            processed = processed.crop(crop_bbox)
            operations.append("auto_crop_conservative")
        else:
            operations.append("auto_crop_noop")
    else:
        operations.append("auto_crop_disabled")

    processed = ImageOps.autocontrast(processed, cutoff=0.5)
    operations.append("autocontrast_cutoff_0_5")
    crop_info = {
        "original_size": original_size,
        "output_size": list(processed.size),
        "pre_deskew_size": pre_deskew_size,
        "post_deskew_size": post_deskew_size,
        "skew_angle_degrees": skew.angle_degrees,
        "skew_confidence": skew.confidence,
        "deskewed": deskewed,
        "deskew_reason": deskew_reason,
        "crop_bbox": list(crop_bbox) if crop_bbox else None,
        "cropped": crop_bbox is not None,
    }
    return processed, operations, crop_info


def _detect_skew(image: Image.Image) -> SkewDetection:
    width, height = image.size
    if width < 30 or height < 30:
        return SkewDetection(None, 0.0, "image too small")

    grayscale = ImageOps.autocontrast(image.convert("L"), cutoff=1)
    histogram = grayscale.histogram()
    total_pixels = width * height
    low = _histogram_percentile(histogram, total_pixels, 0.05)
    high = _histogram_percentile(histogram, total_pixels, 0.95)
    if high - low < 35:
        return SkewDetection(None, 0.0, "low contrast")

    threshold = max(0, min(255, low + int((high - low) * 0.35)))
    ink = grayscale.point(lambda value: 255 if value <= threshold else 0, mode="L")
    bbox = ink.getbbox()
    if not bbox:
        return SkewDetection(None, 0.0, "blank page")

    ink_ratio = _nonzero_ratio(ink, bbox)
    if ink_ratio < 0.002:
        return SkewDetection(None, 0.0, "insufficient foreground")
    if ink_ratio > 0.65:
        return SkewDetection(None, 0.0, "foreground too dense")

    sample = ink.crop(bbox)
    sample.thumbnail((700, 700), Image.Resampling.BILINEAR)
    background = 0
    scores: list[tuple[float, float]] = []
    for correction_angle in _frange(-7.0, 7.0, 0.25):
        rotated = sample.rotate(correction_angle, resample=Image.Resampling.BILINEAR, expand=True, fillcolor=background)
        scores.append((correction_angle, _horizontal_projection_variance(rotated)))

    scores.sort(key=lambda item: item[1], reverse=True)
    best_angle, best_score = scores[0]
    runner_up = max(score for angle, score in scores if abs(angle - best_angle) >= 1.0)
    confidence = 0.0 if best_score <= 0 else max(0.0, min(1.0, (best_score - runner_up) / best_score))
    skew_angle = round(-best_angle, 2)
    return SkewDetection(skew_angle, round(confidence, 3), "skew detected")


def _histogram_percentile(histogram: list[int], total: int, percentile: float) -> int:
    target = total * percentile
    running = 0
    for value, count in enumerate(histogram):
        running += count
        if running >= target:
            return value
    return 255


def _nonzero_ratio(image: Image.Image, bbox: tuple[int, int, int, int]) -> float:
    histogram = image.crop(bbox).histogram()
    foreground = sum(histogram[1:])
    area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
    return foreground / area if area else 0.0


def _frange(start: float, stop: float, step: float) -> list[float]:
    count = int(round((stop - start) / step))
    return [start + index * step for index in range(count + 1)]


def _horizontal_projection_variance(image: Image.Image) -> float:
    width, height = image.size
    pixels = image.load()
    row_counts = []
    for y in range(height):
        row_counts.append(sum(1 for x in range(width) if pixels[x, y] > 0))
    mean = sum(row_counts) / len(row_counts)
    return sum((count - mean) ** 2 for count in row_counts) / len(row_counts)


def _rotate_for_deskew(image: Image.Image, correction_angle: float) -> Image.Image:
    fill = _corner_background_value(image.convert("L"))
    fillcolor: int | tuple[int, int, int]
    if image.mode == "RGB":
        fillcolor = (fill, fill, fill)
    else:
        fillcolor = fill
    return image.rotate(correction_angle, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=fillcolor)


def _detect_conservative_crop_bbox(image: Image.Image) -> tuple[int, int, int, int] | None:
    width, height = image.size
    if width < 20 or height < 20:
        return None

    grayscale = image.convert("L")
    background = _corner_background_value(grayscale)
    diff = grayscale.point(lambda value: 255 if abs(value - background) >= 18 else 0)
    bbox = diff.getbbox()
    if not bbox:
        return None

    left, top, right, bottom = bbox
    if min(left, top, width - right, height - bottom) < 2:
        return None

    crop_width = right - left
    crop_height = bottom - top
    crop_area_ratio = (crop_width * crop_height) / (width * height)
    if crop_area_ratio < 0.25 or crop_area_ratio > 0.98:
        return None

    return bbox


def _corner_background_value(image: Image.Image) -> int:
    width, height = image.size
    sample = max(3, min(width, height) // 20)
    corners = [
        image.crop((0, 0, sample, sample)),
        image.crop((width - sample, 0, width, sample)),
        image.crop((0, height - sample, sample, height)),
        image.crop((width - sample, height - sample, width, height)),
    ]
    values = []
    for corner in corners:
        histogram = corner.histogram()
        total = sum(value * count for value, count in enumerate(histogram))
        values.append(int(round(total / (sample * sample))))
    return sorted(values)[len(values) // 2]


def _save_image(image: Image.Image, target: Path, source_image: Image.Image) -> None:
    save_kwargs: dict[str, Any] = {}
    dpi = source_image.info.get("dpi")
    if dpi:
        save_kwargs["dpi"] = dpi

    suffix = target.suffix.lower()
    if suffix in {".jpg", ".jpeg", ".jpe", ".jfif"}:
        if image.mode != "RGB":
            image = image.convert("RGB")
        save_kwargs.update({"quality": 95})
    image.save(target, **save_kwargs)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
