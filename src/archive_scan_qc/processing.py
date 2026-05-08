"""Local derivative-image processing for scanned-image batches.

The processing layer never modifies source images. It writes derivative files
and a manifest that links each output back to the original scan record.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError

from .concurrency import resolve_worker_count, worker_metadata


@dataclass(frozen=True)
class ProcessingOptions:
    auto_crop: bool = False
    deskew: bool = False
    trim_dark_border: bool = False
    despeckle: bool = False
    deskew_max_degrees: float = 5.0
    deskew_min_confidence: float = 0.08
    workers: int | None = None


@dataclass(frozen=True)
class SkewDetection:
    angle_degrees: float | None
    confidence: float
    reason: str


@dataclass(frozen=True)
class DarkBorderDetection:
    bbox: tuple[int, int, int, int] | None
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
    process_workers = resolve_worker_count(options.workers, len(report["files"]))
    records = _process_records(report["files"], input_dir, image_root, options, process_workers)
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
        workers=worker_metadata(options.workers, process_workers),
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
            "workers": process_workers,
            "worker_mode": performance["mode"],
        },
        "performance": performance,
        "operations": [
            "exif_transpose",
            "convert_non_l_or_rgb_to_rgb",
            "skew_detect_projection",
            "deskew_conservative" if options.deskew else "deskew_disabled",
            "dark_border_trim_conservative" if options.trim_dark_border else "dark_border_trim_disabled",
            "auto_crop_conservative" if options.auto_crop else "auto_crop_disabled",
            "despeckle_isolated_pixels" if options.despeckle else "despeckle_disabled",
            "autocontrast_cutoff_0_5",
            "preserve_source_relative_path",
        ],
        "files": records,
    }
    process_dir.mkdir(parents=True, exist_ok=True)
    (process_dir / "processing_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def _process_records(
    files: list[dict[str, Any]],
    input_dir: Path,
    image_root: Path,
    options: ProcessingOptions,
    workers: int,
) -> list[dict[str, Any]]:
    if workers == 1:
        return [_process_record(item, input_dir, image_root, options) for item in files]
    with ThreadPoolExecutor(max_workers=workers) as executor:
        return list(executor.map(lambda item: _process_record(item, input_dir, image_root, options), files))


def _performance_summary(
    started_at: datetime,
    finished_at: datetime,
    elapsed_seconds: float,
    *,
    total_files: int,
    processed_files: int,
    skipped_files: int,
    failed_files: int,
    workers: dict[str, Any],
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
        **workers,
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
        "dark_border_trimmed": False,
        "dark_border_bbox": None,
        "dark_border_reason": None,
        "crop_bbox": None,
        "cropped": False,
        "despeckled": False,
        "despeckle_pixels_changed": 0,
        "despeckle_reason": None,
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
        if source.resolve() == target.resolve():
            raise ValueError("derivative target would overwrite the source image")
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
                "dark_border_trimmed": process_info["dark_border_trimmed"],
                "dark_border_bbox": process_info["dark_border_bbox"],
                "dark_border_reason": process_info["dark_border_reason"],
                "crop_bbox": process_info["crop_bbox"],
                "cropped": process_info["cropped"],
                "despeckled": process_info["despeckled"],
                "despeckle_pixels_changed": process_info["despeckle_pixels_changed"],
                "despeckle_reason": process_info["despeckle_reason"],
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

    dark_border = DarkBorderDetection(None, "dark border trim disabled")
    dark_border_trimmed = False
    if options.trim_dark_border:
        dark_border = _detect_dark_border_bbox(processed)
        if dark_border.bbox:
            processed = processed.crop(dark_border.bbox)
            operations.append("dark_border_trim_conservative")
            dark_border_trimmed = True
        else:
            operations.append("dark_border_trim_noop")
    else:
        operations.append("dark_border_trim_disabled")

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

    despeckled = False
    despeckle_pixels_changed = 0
    despeckle_reason = "despeckle disabled"
    if options.despeckle:
        processed, despeckle_pixels_changed = _despeckle_isolated_pixels(processed)
        if despeckle_pixels_changed:
            operations.append("despeckle_isolated_pixels")
            despeckled = True
            despeckle_reason = "isolated dark pixels replaced"
        else:
            operations.append("despeckle_noop")
            despeckle_reason = "no isolated dark pixels found"
    else:
        operations.append("despeckle_disabled")

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
        "dark_border_trimmed": dark_border_trimmed,
        "dark_border_bbox": list(dark_border.bbox) if dark_border.bbox else None,
        "dark_border_reason": dark_border.reason,
        "crop_bbox": list(crop_bbox) if crop_bbox else None,
        "cropped": crop_bbox is not None,
        "despeckled": despeckled,
        "despeckle_pixels_changed": despeckle_pixels_changed,
        "despeckle_reason": despeckle_reason,
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


def _detect_dark_border_bbox(image: Image.Image) -> DarkBorderDetection:
    width, height = image.size
    if width < 40 or height < 40:
        return DarkBorderDetection(None, "image too small")

    grayscale = image.convert("L")
    max_x = max(2, int(width * 0.08))
    max_y = max(2, int(height * 0.08))
    min_retain_width = int(width * 0.88)
    min_retain_height = int(height * 0.88)
    left = _dark_edge_run(grayscale, "left", max_x)
    right = _dark_edge_run(grayscale, "right", max_x)
    top = _dark_edge_run(grayscale, "top", max_y)
    bottom = _dark_edge_run(grayscale, "bottom", max_y)

    if max(left, right, top, bottom) < 2:
        return DarkBorderDetection(None, "no confident dark edge border")

    bbox = (left, top, width - right, height - bottom)
    retained_width = bbox[2] - bbox[0]
    retained_height = bbox[3] - bbox[1]
    if retained_width < min_retain_width or retained_height < min_retain_height:
        return DarkBorderDetection(None, "candidate trim exceeds conservative retain ratio")
    if retained_width <= 0 or retained_height <= 0:
        return DarkBorderDetection(None, "invalid trim candidate")

    return DarkBorderDetection(bbox, "dark edge border trimmed")


def _dark_edge_run(image: Image.Image, side: str, max_pixels: int) -> int:
    width, height = image.size
    pixels = image.load()
    run = 0
    for offset in range(max_pixels):
        if side == "left":
            values = [pixels[offset, y] for y in range(height)]
        elif side == "right":
            values = [pixels[width - 1 - offset, y] for y in range(height)]
        elif side == "top":
            values = [pixels[x, offset] for x in range(width)]
        else:
            values = [pixels[x, height - 1 - offset] for x in range(width)]
        dark_ratio = sum(1 for value in values if value <= 45) / len(values)
        mean = sum(values) / len(values)
        if dark_ratio >= 0.72 and mean <= 80:
            run = offset + 1
        else:
            break
    return run


def _despeckle_isolated_pixels(image: Image.Image) -> tuple[Image.Image, int]:
    grayscale = image.convert("L")
    width, height = grayscale.size
    if width < 3 or height < 3:
        return image.copy(), 0

    gray_pixels = grayscale.load()
    source = image.convert("RGB") if image.mode != "RGB" else image.copy()
    output = source.copy()
    source_pixels = source.load()
    output_pixels = output.load()
    changed = 0
    for y in range(1, height - 1):
        for x in range(1, width - 1):
            if gray_pixels[x, y] > 60:
                continue
            dark_neighbors = 0
            neighbor_values: list[int] = []
            neighbor_rgb: list[tuple[int, int, int]] = []
            for ny in range(y - 1, y + 2):
                for nx in range(x - 1, x + 2):
                    if nx == x and ny == y:
                        continue
                    value = gray_pixels[nx, ny]
                    neighbor_values.append(value)
                    neighbor_rgb.append(source_pixels[nx, ny])
                    if value <= 90:
                        dark_neighbors += 1
            if dark_neighbors > 1:
                continue
            wider_dark = 0
            for ny in range(max(0, y - 2), min(height, y + 3)):
                for nx in range(max(0, x - 2), min(width, x + 3)):
                    if nx == x and ny == y:
                        continue
                    if gray_pixels[nx, ny] <= 90:
                        wider_dark += 1
            if wider_dark > 2:
                continue
            median_gray = sorted(neighbor_values)[len(neighbor_values) // 2]
            if median_gray < 120:
                continue
            replacement = tuple(sorted(channel)[len(channel) // 2] for channel in zip(*neighbor_rgb))
            output_pixels[x, y] = replacement
            changed += 1

    if image.mode == "L":
        return output.convert("L"), changed
    if image.mode == "RGB":
        return output, changed
    return output.convert(image.mode), changed


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
