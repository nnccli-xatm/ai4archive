"""Local OCR preprocessing validation with public-safe aggregate output."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import random
import re
import shutil
import subprocess
import tempfile
from typing import Any

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .processing import ProcessingOptions, process_images
from .scanner import ScanConfig, scan_batch


OCR_PROVIDER_PROBE_JSON = "ocr_provider_probe.json"
OCR_PREPROCESSING_OCR_VALIDATION_JSON = "ocr_preprocessing_ocr_validation_summary.json"
OCR_PROVIDER_PROBE_SCHEMA_VERSION = "scan-qc.ocr-provider-probe.v1"
OCR_PREPROCESSING_OCR_VALIDATION_SCHEMA_VERSION = "scan-qc.ocr-preprocessing-ocr-validation.v1"

_KNOWN_TEXT = (
    "OCR PREPROCESS SAMPLE 456",
    "SCAN QUALITY SAMPLE 789",
    "TEXT CLEANUP SAMPLE 123",
)
_PROVIDER_IDS = {"disabled", "tesseract"}


@dataclass(frozen=True)
class OcrProviderProbeConfig:
    provider: str = "disabled"
    command: str | None = None


@dataclass(frozen=True)
class OcrValidationConfig:
    output_dir: Path
    provider: str = "disabled"
    provider_command: str | None = None
    require_ocr_metric: bool = False
    min_cer_relative_reduction: float = 0.25
    min_wer_relative_reduction: float = 0.0
    generated_at: str | None = None


def write_ocr_provider_probe(
    *,
    output_path: Path | None = None,
    config: OcrProviderProbeConfig | None = None,
) -> tuple[Path, dict[str, Any]]:
    payload = build_ocr_provider_probe(config or OcrProviderProbeConfig())
    path = output_path or (Path.cwd() / OCR_PROVIDER_PROBE_JSON)
    if path.suffix == "":
        path = path / OCR_PROVIDER_PROBE_JSON
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path, payload


def build_ocr_provider_probe(config: OcrProviderProbeConfig | None = None) -> dict[str, Any]:
    config = config or OcrProviderProbeConfig()
    provider = _normalized_provider(config.provider)
    generated_at = datetime.now(timezone.utc).isoformat()
    if provider == "disabled":
        return {
            "schema_version": OCR_PROVIDER_PROBE_SCHEMA_VERSION,
            "generated_at": generated_at,
            "provider": "disabled",
            "status": "disabled",
            "available": False,
            "can_run_ocr": False,
            "risk_codes": ["provider_disabled_by_default"],
            "privacy": _privacy_payload(),
        }

    command = config.command or shutil.which("tesseract")
    if not command:
        return {
            "schema_version": OCR_PROVIDER_PROBE_SCHEMA_VERSION,
            "generated_at": generated_at,
            "provider": "tesseract",
            "status": "unavailable",
            "available": False,
            "can_run_ocr": False,
            "risk_codes": ["provider_executable_missing"],
            "privacy": _privacy_payload(),
        }

    try:
        completed = subprocess.run(
            [command, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {
            "schema_version": OCR_PROVIDER_PROBE_SCHEMA_VERSION,
            "generated_at": generated_at,
            "provider": "tesseract",
            "status": "unavailable",
            "available": False,
            "can_run_ocr": False,
            "risk_codes": ["provider_probe_failed"],
            "privacy": _privacy_payload(),
        }

    version_line = _safe_version_line(completed.stdout)
    available = completed.returncode == 0 and bool(version_line)
    return {
        "schema_version": OCR_PROVIDER_PROBE_SCHEMA_VERSION,
        "generated_at": generated_at,
        "provider": "tesseract",
        "status": "available" if available else "unavailable",
        "available": available,
        "can_run_ocr": available,
        "version": version_line,
        "risk_codes": [] if available else ["provider_probe_failed"],
        "privacy": _privacy_payload(),
    }


def write_ocr_preprocessing_ocr_validation(config: OcrValidationConfig) -> tuple[Path, dict[str, Any]]:
    summary = build_ocr_preprocessing_ocr_validation(config)
    path = config.output_dir / OCR_PREPROCESSING_OCR_VALIDATION_JSON
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path, summary


def build_ocr_preprocessing_ocr_validation(config: OcrValidationConfig) -> dict[str, Any]:
    provider = _normalized_provider(config.provider)
    output_dir = config.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    provider_probe = build_ocr_provider_probe(OcrProviderProbeConfig(provider, config.provider_command))

    with tempfile.TemporaryDirectory(prefix="ocr-validation-", dir=str(output_dir)) as temp_dir:
        root = Path(temp_dir)
        input_dir = root / "input"
        scan_dir = root / "scan"
        derivative_dir = root / "derivatives"
        input_dir.mkdir()
        _write_synthetic_ocr_pages(input_dir)
        report = scan_batch(ScanConfig("ocr-validation", "synthetic-known-text", input_dir, scan_dir, workers=1))
        manifest = process_images(
            report,
            input_dir,
            derivative_dir,
            ProcessingOptions(
                ocr_preprocess=True,
                ocr_binary=True,
                processing_profile="ocr_preprocess",
                despeckle=False,
                workers=1,
                crop_margin_mm=0.0,
            ),
        )
        text_metrics = _ocr_text_metrics(
            input_dir=input_dir,
            derivative_dir=derivative_dir,
            manifest=manifest,
            provider_probe=provider_probe,
            command=config.provider_command,
        )
        visual_metrics = _visual_proxy_metrics(input_dir=input_dir, derivative_dir=derivative_dir, manifest=manifest)

    blocking_codes: list[str] = []
    if manifest.get("summary", {}).get("failed_files"):
        blocking_codes.append("processing_failed")
    if config.require_ocr_metric and not text_metrics["available"]:
        blocking_codes.append("ocr_metric_unavailable")
    if text_metrics["available"]:
        cer_reduction = text_metrics["cer_relative_reduction"]
        wer_reduction = text_metrics["wer_relative_reduction"]
        if cer_reduction is None or cer_reduction < config.min_cer_relative_reduction:
            blocking_codes.append("cer_reduction_below_threshold")
        if wer_reduction is None or wer_reduction < config.min_wer_relative_reduction:
            blocking_codes.append("wer_reduction_below_threshold")
    status = "pass" if not blocking_codes else "fail"
    return {
        "schema_version": OCR_PREPROCESSING_OCR_VALIDATION_SCHEMA_VERSION,
        "generated_at": config.generated_at or datetime.now(timezone.utc).isoformat(),
        "status": status,
        "blocking_codes": sorted(set(blocking_codes)),
        "provider_probe": provider_probe,
        "synthetic_fixture": {
            "page_count": len(_KNOWN_TEXT),
            "known_text_included": False,
            "source_images_included": False,
            "row_level_records_included": False,
        },
        "thresholds": {
            "min_cer_relative_reduction": config.min_cer_relative_reduction,
            "min_wer_relative_reduction": config.min_wer_relative_reduction,
            "require_ocr_metric": config.require_ocr_metric,
        },
        "processing_counts": {
            "processed_files": int(manifest.get("summary", {}).get("processed_files", 0)),
            "failed_files": int(manifest.get("summary", {}).get("failed_files", 0)),
            "ocr_preprocessed_files": sum(
                1 for record in manifest.get("files", []) if isinstance(record, dict) and record.get("ocr_preprocessed")
            ),
            "ocr_binary_created_files": sum(
                1 for record in manifest.get("files", []) if isinstance(record, dict) and record.get("ocr_binary_created")
            ),
        },
        "ocr_text_metrics": text_metrics,
        "visual_proxy_metrics": visual_metrics,
        "privacy": _privacy_payload(),
    }


def _ocr_text_metrics(
    *,
    input_dir: Path,
    derivative_dir: Path,
    manifest: dict[str, Any],
    provider_probe: dict[str, Any],
    command: str | None,
) -> dict[str, Any]:
    if not provider_probe.get("can_run_ocr"):
        return {
            "available": False,
            "provider": provider_probe.get("provider"),
            "source_cer_macro": None,
            "processed_cer_macro": None,
            "cer_relative_reduction": None,
            "source_wer_macro": None,
            "processed_wer_macro": None,
            "wer_relative_reduction": None,
        }

    source_cer: list[float] = []
    processed_gray_cer: list[float] = []
    processed_binary_cer: list[float] = []
    best_processed_cer: list[float] = []
    source_wer: list[float] = []
    processed_gray_wer: list[float] = []
    processed_binary_wer: list[float] = []
    best_processed_wer: list[float] = []
    best_output_counts = {"gray": 0, "binary": 0}
    records = [record for record in manifest.get("files", []) if isinstance(record, dict)]
    for index, record in enumerate(records):
        expected = _KNOWN_TEXT[index] if index < len(_KNOWN_TEXT) else ""
        source_rel = record.get("source_relative_path")
        output_rel = record.get("output_relative_path")
        if not isinstance(source_rel, str) or not isinstance(output_rel, str):
            continue
        source_text = _run_tesseract(input_dir / source_rel, command=command)
        gray_text = _run_tesseract(derivative_dir / output_rel, command=command)
        binary_rel = record.get("ocr_binary_output_relative_path")
        binary_text = (
            _run_tesseract(derivative_dir / binary_rel, command=command)
            if isinstance(binary_rel, str)
            else None
        )
        if source_text is None or gray_text is None:
            continue
        source = _text_error_rates(expected, source_text)
        gray = _text_error_rates(expected, gray_text)
        binary = _text_error_rates(expected, binary_text) if binary_text is not None else None
        best = gray
        best_label = "gray"
        if binary is not None and binary["cer"] <= gray["cer"]:
            best = binary
            best_label = "binary"
        source_cer.append(source["cer"])
        processed_gray_cer.append(gray["cer"])
        if binary is not None:
            processed_binary_cer.append(binary["cer"])
        best_processed_cer.append(best["cer"])
        source_wer.append(source["wer"])
        processed_gray_wer.append(gray["wer"])
        if binary is not None:
            processed_binary_wer.append(binary["wer"])
        best_processed_wer.append(best["wer"])
        best_output_counts[best_label] += 1

    source_cer_macro = _mean(source_cer)
    processed_gray_cer_macro = _mean(processed_gray_cer)
    processed_binary_cer_macro = _mean(processed_binary_cer)
    best_processed_cer_macro = _mean(best_processed_cer)
    source_wer_macro = _mean(source_wer)
    processed_gray_wer_macro = _mean(processed_gray_wer)
    processed_binary_wer_macro = _mean(processed_binary_wer)
    best_processed_wer_macro = _mean(best_processed_wer)
    return {
        "available": bool(source_cer and best_processed_cer),
        "provider": provider_probe.get("provider"),
        "page_count": min(len(source_cer), len(best_processed_cer)),
        "source_cer_macro": _rounded(source_cer_macro),
        "processed_cer_macro": _rounded(best_processed_cer_macro),
        "processed_gray_cer_macro": _rounded(processed_gray_cer_macro),
        "processed_binary_cer_macro": _rounded(processed_binary_cer_macro),
        "cer_relative_reduction": _rounded(_relative_reduction(source_cer_macro, best_processed_cer_macro)),
        "source_wer_macro": _rounded(source_wer_macro),
        "processed_wer_macro": _rounded(best_processed_wer_macro),
        "processed_gray_wer_macro": _rounded(processed_gray_wer_macro),
        "processed_binary_wer_macro": _rounded(processed_binary_wer_macro),
        "wer_relative_reduction": _rounded(_relative_reduction(source_wer_macro, best_processed_wer_macro)),
        "best_output_counts": best_output_counts,
    }


def _visual_proxy_metrics(*, input_dir: Path, derivative_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    source_contrast: list[float] = []
    processed_contrast: list[float] = []
    changed: list[float] = []
    for record in manifest.get("files", []):
        if not isinstance(record, dict):
            continue
        source_rel = record.get("source_relative_path")
        output_rel = record.get("output_relative_path")
        if not isinstance(source_rel, str) or not isinstance(output_rel, str):
            continue
        source_contrast.append(_text_background_contrast(input_dir / source_rel))
        processed_contrast.append(_text_background_contrast(derivative_dir / output_rel))
        audit = record.get("processing_audit") if isinstance(record.get("processing_audit"), dict) else {}
        value = audit.get("ocr_preprocess_changed_pixel_ratio")
        if isinstance(value, int | float) and math.isfinite(float(value)):
            changed.append(float(value))
    source_macro = _mean(source_contrast)
    processed_macro = _mean(processed_contrast)
    return {
        "source_text_background_contrast_macro": _rounded(source_macro),
        "processed_text_background_contrast_macro": _rounded(processed_macro),
        "text_background_contrast_delta": _rounded(
            None if source_macro is None or processed_macro is None else processed_macro - source_macro
        ),
        "ocr_preprocess_changed_pixel_ratio_macro": _rounded(_mean(changed)),
    }


def _write_synthetic_ocr_pages(input_dir: Path) -> None:
    font = _load_validation_font()
    for index, text in enumerate(_KNOWN_TEXT, start=1):
        image = Image.new("L", (880, 220), 214)
        pixels = image.load()
        for y in range(image.height):
            gradient = int((y / max(1, image.height - 1)) * 10)
            for x in range(image.width):
                pixels[x, y] = max(0, min(255, 208 + gradient))
        rng = random.Random(index)
        for y in range(42, 156):
            for x in range(30, 810):
                texture = rng.randint(-10, 10)
                if rng.random() < 0.035:
                    texture -= rng.randint(10, 24)
                pixels[x, y] = max(0, min(255, 176 + texture))
        draw = ImageDraw.Draw(image)
        draw.text((44, 70), text, fill=98, font=font)
        image = image.filter(ImageFilter.GaussianBlur(radius=0.65))
        image.save(input_dir / f"ocr_synthetic_{index:02d}.png")


def _load_validation_font() -> ImageFont.ImageFont:
    candidates = [
        Path(r"C:\Windows\Fonts\arial.ttf"),
        Path(r"C:\Windows\Fonts\calibri.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    for path in candidates:
        if path.is_file():
            try:
                return ImageFont.truetype(str(path), 34)
            except OSError:
                continue
    return ImageFont.load_default()


def _run_tesseract(path: Path, *, command: str | None) -> str | None:
    executable = command or shutil.which("tesseract")
    if not executable:
        return None
    try:
        completed = subprocess.run(
            [executable, str(path), "stdout", "--psm", "6", "-l", "eng"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def _text_error_rates(expected: str, actual: str) -> dict[str, float]:
    expected_chars = _normalize_text(expected)
    actual_chars = _normalize_text(actual)
    expected_words = expected_chars.split()
    actual_words = actual_chars.split()
    return {
        "cer": _edit_distance(list(expected_chars), list(actual_chars)) / max(1, len(expected_chars)),
        "wer": _edit_distance(expected_words, actual_words) / max(1, len(expected_words)),
    }


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9 ]+", " ", value.upper())).strip()


def _edit_distance(left: list[str], right: list[str]) -> int:
    if not left:
        return len(right)
    if not right:
        return len(left)
    previous = list(range(len(right) + 1))
    for i, left_item in enumerate(left, start=1):
        current = [i]
        for j, right_item in enumerate(right, start=1):
            current.append(
                min(
                    previous[j] + 1,
                    current[j - 1] + 1,
                    previous[j - 1] + (0 if left_item == right_item else 1),
                )
            )
        previous = current
    return previous[-1]


def _text_background_contrast(path: Path) -> float:
    with Image.open(path) as image:
        gray = image.convert("L")
        histogram = gray.histogram()
    total = max(1, gray.width * gray.height)
    dark_weight = sum(histogram[:128])
    background_weight = sum(histogram[170:])
    if dark_weight <= 0 or background_weight <= 0:
        return 0.0
    dark_mean = sum(value * histogram[value] for value in range(128)) / dark_weight
    background_mean = sum(value * histogram[value] for value in range(170, 256)) / background_weight
    return background_mean - dark_mean


def _normalized_provider(value: str) -> str:
    provider = value.strip().lower()
    if provider not in _PROVIDER_IDS:
        raise ValueError(f"unsupported OCR provider: {value}")
    return provider


def _safe_version_line(stdout: str) -> str | None:
    first = next((line.strip() for line in stdout.splitlines() if line.strip()), None)
    if first is None:
        return None
    return re.sub(r"[^A-Za-z0-9 ._:+-]", "", first)[:80]


def _relative_reduction(source: float | None, processed: float | None) -> float | None:
    if source is None or processed is None or source <= 0:
        return None
    return (source - processed) / source


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _rounded(value: float | None) -> float | None:
    if value is None or not math.isfinite(float(value)):
        return None
    return round(float(value), 6)


def _privacy_payload() -> dict[str, Any]:
    return {
        "public_safe": True,
        "aggregate_only": True,
        "contains_paths": False,
        "contains_file_names": False,
        "contains_hashes": False,
        "contains_ocr_text": False,
        "contains_image_content": False,
        "contains_row_level_records": False,
    }
