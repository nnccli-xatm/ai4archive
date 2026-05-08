#!/usr/bin/env python3
"""Local-only OCR importer for archive privacy POC.

This script uses macOS Vision through `ocrmac`. It does not call cloud OCR
services and does not upload images. OCR text is sensitive; outputs should stay
on a trusted local disk.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Iterable

from ocrmac.ocrmac import OCR


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}


def iter_images(root: Path, *, recursive: bool) -> Iterable[Path]:
    pattern = "**/*" if recursive else "*"
    for path in sorted(root.glob(pattern)):
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS:
            yield path


def ocr_image(path: Path, *, languages: list[str]) -> tuple[str, list[dict[str, object]]]:
    ocr = OCR(
        str(path),
        framework="vision",
        recognition_level="accurate",
        language_preference=languages or None,
        detail=True,
    )
    observations = ocr.recognize(px=True)
    lines: list[str] = []
    blocks: list[dict[str, object]] = []
    for item in observations:
        if not item:
            continue
        text = str(item[0]).strip()
        if not text:
            continue
        confidence = float(item[1]) if len(item) > 1 and item[1] is not None else None
        bbox = item[2] if len(item) > 2 else None
        lines.append(text)
        blocks.append(
            {
                "text": text,
                "confidence": confidence,
                "bbox": list(bbox) if bbox is not None else None,
            }
        )
    return "\n".join(lines), blocks


def main() -> int:
    parser = argparse.ArgumentParser(description="OCR local images into POC JSONL.")
    parser.add_argument("--image-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, default=Path("out-ocr-local"))
    parser.add_argument("--recursive", action="store_true")
    parser.add_argument("--max-files", type=int, default=20)
    parser.add_argument(
        "--languages",
        default="zh-Hans,en-US",
        help="Comma-separated Vision language preferences. Default: zh-Hans,en-US",
    )
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    dataset_path = args.out_dir / "ocr_dataset.jsonl"
    blocks_path = args.out_dir / "ocr_blocks_private.jsonl"
    manifest_path = args.out_dir / "manifest_private.jsonl"

    languages = [item.strip() for item in args.languages.split(",") if item.strip()]
    images = list(iter_images(args.image_dir, recursive=args.recursive))
    if args.max_files and args.max_files > 0:
        images = images[: args.max_files]

    manifest: list[dict[str, object]] = []
    with dataset_path.open("w", encoding="utf-8") as dataset, blocks_path.open(
        "w", encoding="utf-8"
    ) as block_out:
        for idx, image_path in enumerate(images, start=1):
            record_id = f"ocr-{idx:04d}"
            text, blocks = ocr_image(image_path, languages=languages)
            dataset_record = {
                "id": record_id,
                "title": "local OCR image",
                "text": text,
                "gold_mentions": [],
            }
            dataset.write(json.dumps(dataset_record, ensure_ascii=False) + "\n")
            block_out.write(
                json.dumps({"id": record_id, "blocks": blocks}, ensure_ascii=False)
                + "\n"
            )
            manifest.append(
                {
                    "id": record_id,
                    "path": str(image_path),
                    "bytes": image_path.stat().st_size,
                    "ocr_chars": len(text),
                    "ocr_blocks": len(blocks),
                }
            )

    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {
        "image_dir": str(args.image_dir),
        "image_count": len(images),
        "dataset": str(dataset_path),
        "blocks_private": str(blocks_path),
        "manifest_private": str(manifest_path),
        "languages": languages,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
