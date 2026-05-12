"""Synthetic one-command rehearsal for the local production workflow."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from .local_workbench import DEFAULT_METADATA_DIRNAME
from .processing_review import REVIEW_JSON as PROCESSING_REVIEW_JSON, write_processing_review_package
from .production_review_queue import PRODUCTION_REVIEW_QUEUE_JSON, write_production_review_queue
from .production_runner import PRODUCTION_RUN_PROGRESS_JSON, PRODUCTION_RUN_SUMMARY_JSON, ProductionRunConfig, run_production_folder


SYNTHETIC_INPUT_DIRNAME = "synthetic_input"
DERIVATIVES_DIRNAME = "derivatives"
SCHEMA_VERSION = "scan-qc.production-rehearsal.v1"


@dataclass(frozen=True)
class ProductionRehearsalConfig:
    root_dir: Path | None = None
    project_id: str = "local-rehearsal"
    batch_id: str = "synthetic-batch"
    workers: int | None = 1
    keep_existing: bool = False


def run_production_rehearsal(config: ProductionRehearsalConfig) -> dict[str, Any]:
    """Create synthetic source images, run production processing, and build workbench artifacts."""
    root_dir = _prepare_root(config.root_dir, keep_existing=config.keep_existing)
    input_dir = root_dir / SYNTHETIC_INPUT_DIRNAME
    derivatives_dir = root_dir / DERIVATIVES_DIRNAME
    metadata_dir = derivatives_dir / DEFAULT_METADATA_DIRNAME
    input_dir.mkdir(parents=True, exist_ok=True)
    derivatives_dir.mkdir(parents=True, exist_ok=True)
    metadata_dir.mkdir(parents=True, exist_ok=True)

    generated_sources = write_synthetic_rehearsal_images(input_dir)
    summary = run_production_folder(
        ProductionRunConfig(
            input_dir=input_dir,
            derivative_output_dir=derivatives_dir,
            metadata_output_dir=metadata_dir,
            project_id=config.project_id,
            batch_id=config.batch_id,
            auto_crop=True,
            deskew=True,
            trim_dark_border=True,
            despeckle=True,
            resume_processing=True,
            reuse_scan_measurements=True,
            workers=config.workers,
        )
    )
    processing_review_path = _write_processing_review_if_available(derivatives_dir, metadata_dir)
    scan_report_path = _artifact_path(summary, "admin_scan_report")
    queue_path, queue = write_production_review_queue(
        metadata_dir / PRODUCTION_REVIEW_QUEUE_JSON,
        scan_qc_report_path=scan_report_path if scan_report_path and scan_report_path.exists() else None,
        processing_review_package_path=processing_review_path if processing_review_path and processing_review_path.exists() else None,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now().astimezone().isoformat(),
        "root_dir": str(root_dir),
        "input_dir": str(input_dir),
        "derivatives_dir": str(derivatives_dir),
        "metadata_dir": str(metadata_dir),
        "generated_sources": [str(path) for path in generated_sources],
        "summary_path": str(metadata_dir / PRODUCTION_RUN_SUMMARY_JSON),
        "progress_path": str(metadata_dir / PRODUCTION_RUN_PROGRESS_JSON),
        "review_queue_path": str(queue_path),
        "processing_review_path": str(processing_review_path) if processing_review_path else None,
        "status": summary["status"],
        "status_label_zh": summary["status_label_zh"],
        "operator_message_zh": summary["operator_summary"]["message_zh"],
        "source_count": summary["operator_summary"]["total_source_images"],
        "derivative_count": summary["operator_summary"]["derivative_images_ready"],
        "review_queue_items": queue["summary"]["total_items"],
        "network_services_called": False,
        "model_inference_run": False,
        "privacy": {
            "synthetic_only": True,
            "private_images_required": False,
            "external_services_required": False,
        },
    }


def write_synthetic_rehearsal_images(input_dir: Path) -> list[Path]:
    input_dir.mkdir(parents=True, exist_ok=True)
    pages = [
        ("SYNTHETIC_BATCH_0001_clean.png", _synthetic_page("0001", skew=False), (300, 300)),
        ("SYNTHETIC_BATCH_0002_skew.png", _synthetic_page("0002", skew=True), (300, 300)),
        ("SYNTHETIC_BATCH_0003_low_dpi.png", _synthetic_page("0003", skew=False), (150, 150)),
    ]
    paths: list[Path] = []
    for filename, image, dpi in pages:
        path = input_dir / filename
        image.save(path, dpi=dpi)
        paths.append(path)
    return paths


def _prepare_root(root_dir: Path | None, *, keep_existing: bool) -> Path:
    if root_dir is None:
        return Path(tempfile.mkdtemp(prefix="archive-scan-qc-rehearsal-")).resolve()
    resolved = root_dir.expanduser().resolve()
    if resolved.exists() and not keep_existing and any(resolved.iterdir()):
        raise ValueError("演练输出文件夹已有内容，请换一个空文件夹，或加入 --keep-existing。")
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _synthetic_page(label: str, *, skew: bool) -> Image.Image:
    image = Image.new("RGB", (640, 900), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((44, 42, 596, 858), outline=(210, 210, 210), width=4)
    draw.rectangle((76, 90, 564, 172), fill=(242, 246, 248), outline=(160, 170, 176))
    draw.text((96, 116), f"SYNTHETIC ARCHIVE PAGE {label}", fill=(30, 30, 30))
    for index in range(12):
        y = 230 + index * 42
        draw.line((92, y, 548, y), fill=(60, 60, 60), width=2)
        draw.line((112, y + 16, 492, y + 16), fill=(100, 100, 100), width=1)
    draw.rectangle((32, 36, 50, 864), fill=(28, 28, 28))
    draw.point((520, 720), fill=(0, 0, 0))
    draw.point((522, 724), fill=(0, 0, 0))
    if skew:
        return image.rotate(-2.0, resample=Image.Resampling.BICUBIC, expand=True, fillcolor="white")
    return image


def _write_processing_review_if_available(derivatives_dir: Path, metadata_dir: Path) -> Path | None:
    manifest = derivatives_dir / "processing_manifest.json"
    if not manifest.exists():
        return None
    json_path, _html_path = write_processing_review_package(manifest, metadata_dir)
    return json_path


def _artifact_path(summary: dict[str, Any], key: str) -> Path | None:
    artifacts = summary.get("artifacts")
    if not isinstance(artifacts, dict) or not artifacts.get(key):
        return None
    return Path(str(artifacts[key]))
