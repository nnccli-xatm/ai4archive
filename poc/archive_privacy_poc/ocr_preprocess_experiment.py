#!/usr/bin/env python3
"""Local-only OCR preprocessing experiment for scanned archive images.

This script never calls cloud OCR. It creates derived images and OCR JSONL files
under the chosen output directory; both may contain private information and
should stay on a trusted local disk.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Callable, Iterable

from PIL import Image, ImageFilter, ImageOps
from ocrmac.ocrmac import OCR

from archive_privacy_poc import Span, detect_rules, merge_spans


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}


def iter_images(root: Path, *, recursive: bool) -> Iterable[Path]:
    pattern = "**/*" if recursive else "*"
    for path in sorted(root.glob(pattern)):
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS:
            yield path


def resize_if_needed(image: Image.Image, scale: float) -> Image.Image:
    if scale == 1:
        return image
    width, height = image.size
    return image.resize((int(width * scale), int(height * scale)), Image.Resampling.LANCZOS)


def preprocess_gray2x(image: Image.Image) -> Image.Image:
    out = ImageOps.grayscale(image)
    out = ImageOps.autocontrast(out)
    out = resize_if_needed(out, 2.0)
    return out.filter(ImageFilter.UnsharpMask(radius=1.2, percent=160, threshold=3))


def preprocess_gray3x(image: Image.Image) -> Image.Image:
    out = ImageOps.grayscale(image)
    out = ImageOps.autocontrast(out)
    out = resize_if_needed(out, 3.0)
    return out.filter(ImageFilter.UnsharpMask(radius=1.4, percent=180, threshold=3))


def preprocess_binary2x(image: Image.Image) -> Image.Image:
    out = preprocess_gray2x(image)
    return out.point(lambda pixel: 255 if pixel > 170 else 0)


PREPROCESSORS: dict[str, Callable[[Image.Image], Image.Image] | None] = {
    "original": None,
    "gray2x": preprocess_gray2x,
    "gray3x": preprocess_gray3x,
    "binary2x": preprocess_binary2x,
}


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


def text_profile(text: str) -> dict[str, float | int]:
    total = len(text)
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    ascii_alnum = sum(1 for ch in text if ch.isascii() and ch.isalnum())
    common = sum(1 for ch in text if ch.isspace() or ch in ",.;:，。；：、()（）-_/\\[]【】")
    other = max(0, total - cjk - ascii_alnum - common)
    return {
        "chars": total,
        "cjk_ratio": round(cjk / total, 4) if total else 0,
        "ascii_alnum_ratio": round(ascii_alnum / total, 4) if total else 0,
        "other_ratio": round(other / total, 4) if total else 0,
    }


def summarize_variant(records: list[dict[str, object]]) -> dict[str, object]:
    label_counts: Counter[str] = Counter()
    chars: list[int] = []
    block_counts: list[int] = []
    confidences: list[float] = []
    low_conf_blocks = 0
    very_low_conf_blocks = 0
    docs_with_spans = 0
    docs_with_name = 0

    for record in records:
        text = str(record["text"])
        blocks = record["blocks"]
        spans = merge_spans(detect_rules(text), text)
        if spans:
            docs_with_spans += 1
        if any(span.label == "personal_name" for span in spans):
            docs_with_name += 1
        label_counts.update(span.label for span in spans)
        chars.append(len(text))
        block_counts.append(len(blocks))
        for block in blocks:
            confidence = block.get("confidence")
            if isinstance(confidence, (int, float)):
                confidences.append(float(confidence))
                low_conf_blocks += confidence < 0.5
                very_low_conf_blocks += confidence < 0.3

    return {
        "docs": len(records),
        "ocr_chars_total": sum(chars),
        "ocr_chars_min": min(chars) if chars else 0,
        "ocr_chars_max": max(chars) if chars else 0,
        "ocr_blocks_total": sum(block_counts),
        "avg_confidence": round(sum(confidences) / len(confidences), 4) if confidences else None,
        "low_confidence_blocks_lt_0_5": low_conf_blocks,
        "very_low_confidence_blocks_lt_0_3": very_low_conf_blocks,
        "docs_with_rule_spans": docs_with_spans,
        "docs_with_rule_personal_name": docs_with_name,
        "rule_spans_total": sum(label_counts.values()),
        "rule_label_counts": dict(sorted(label_counts.items())),
    }


def run_variant(
    *,
    variant: str,
    images: list[Path],
    out_dir: Path,
    languages: list[str],
) -> dict[str, object]:
    variant_dir = out_dir / variant
    private_image_dir = variant_dir / "preprocessed_private"
    variant_dir.mkdir(parents=True, exist_ok=True)
    if variant != "original":
        private_image_dir.mkdir(parents=True, exist_ok=True)

    dataset_path = variant_dir / "ocr_dataset.jsonl"
    blocks_path = variant_dir / "ocr_blocks_private.jsonl"
    manifest_path = variant_dir / "manifest_private.jsonl"

    records_for_summary: list[dict[str, object]] = []
    manifest: list[dict[str, object]] = []
    preprocessor = PREPROCESSORS[variant]

    with dataset_path.open("w", encoding="utf-8") as dataset, blocks_path.open(
        "w", encoding="utf-8"
    ) as block_out:
        for idx, source_path in enumerate(images, start=1):
            record_id = f"ocr-{idx:04d}"
            ocr_path = source_path
            if preprocessor is not None:
                with Image.open(source_path) as image:
                    processed = preprocessor(image)
                    ocr_path = private_image_dir / f"{record_id}.png"
                    processed.save(ocr_path)

            text, blocks = ocr_image(ocr_path, languages=languages)
            profile = text_profile(text)
            dataset.write(
                json.dumps(
                    {
                        "id": record_id,
                        "title": f"local OCR image {variant}",
                        "text": text,
                        "gold_mentions": [],
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            )
            block_out.write(
                json.dumps({"id": record_id, "blocks": blocks}, ensure_ascii=False, separators=(",", ":"))
                + "\n"
            )
            manifest.append(
                {
                    "id": record_id,
                    "variant": variant,
                    "source_bytes": source_path.stat().st_size,
                    "ocr_source": "original" if preprocessor is None else "preprocessed_private",
                    **profile,
                    "ocr_blocks": len(blocks),
                }
            )
            records_for_summary.append({"id": record_id, "text": text, "blocks": blocks})

    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "variant": variant,
        "dataset": str(dataset_path),
        "blocks_private": str(blocks_path),
        "manifest_private": str(manifest_path),
        **summarize_variant(records_for_summary),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run local OCR preprocessing experiments.")
    parser.add_argument("--image-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, default=Path("out-ocr-preprocess-local"))
    parser.add_argument("--recursive", action="store_true")
    parser.add_argument("--max-files", type=int, default=20)
    parser.add_argument(
        "--variants",
        default="original,gray2x,gray3x,binary2x",
        help=f"Comma-separated variants. Available: {', '.join(PREPROCESSORS)}",
    )
    parser.add_argument("--languages", default="zh-Hans,en-US")
    args = parser.parse_args()

    variants = [item.strip() for item in args.variants.split(",") if item.strip()]
    unknown = [variant for variant in variants if variant not in PREPROCESSORS]
    if unknown:
        raise ValueError(f"Unknown variants: {', '.join(unknown)}")

    images = list(iter_images(args.image_dir, recursive=args.recursive))
    if args.max_files and args.max_files > 0:
        images = images[: args.max_files]
    languages = [item.strip() for item in args.languages.split(",") if item.strip()]

    args.out_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "note": "Local-only experiment. OCR text and derived images are private and are not included in this summary.",
        "image_count": len(images),
        "languages": languages,
        "variants": [
            run_variant(variant=variant, images=images, out_dir=args.out_dir, languages=languages)
            for variant in variants
        ],
    }
    summary_path = args.out_dir / "experiment_summary_no_text.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"summary": str(summary_path), "image_count": len(images)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
