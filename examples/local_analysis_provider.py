#!/usr/bin/env python3
"""Deterministic local analysis provider example for archive-scan-qc.

This sample implements the JSONL stdin/stdout provider contract without any
model, network, GPU, OCR, ONNX, Paddle, or private-image dependency. It reads
only the minimized metadata records supplied by archive-scan-qc and emits a
synthetic finding for tiny openable images so release validation can prove the
contract end to end.
"""

from __future__ import annotations

import json
import sys
from typing import Any


PROVIDER_METADATA = {
    "name": "local-sample",
    "version": "1.0",
    "model": "deterministic-metadata-only",
    "backend": "python-standard-library",
    "network_required": False,
    "gpu_required": False,
    # Scanner-side metadata sanitization drops keys containing "path".
    "sanitizer_probe_path": "synthetic-value-should-not-appear-in-reports",
}


def main() -> int:
    print(json.dumps({"type": "metadata", "provider": PROVIDER_METADATA}, sort_keys=True), flush=True)
    for line_number, raw_line in enumerate(sys.stdin, start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            print(f"invalid input JSON on line {line_number}: {exc.msg}", file=sys.stderr)
            return 2
        if not isinstance(record, dict):
            print(f"input line {line_number} must be a JSON object", file=sys.stderr)
            return 2
        finding = _synthetic_finding(record)
        if finding is not None:
            print(json.dumps(finding, sort_keys=True), flush=True)
    return 0


def _synthetic_finding(record: dict[str, Any]) -> dict[str, Any] | None:
    relative_path = record.get("relative_path")
    width = _number(record.get("width"))
    height = _number(record.get("height"))
    openable = record.get("openable") is True
    if not isinstance(relative_path, str) or not relative_path or not openable:
        return None
    if width is None or height is None or (width >= 64 and height >= 64):
        return None
    return {
        "type": "finding",
        "relative_path": relative_path,
        "rule": "provider.local-sample.small_canvas",
        "severity": "P2",
        "confidence": 0.66,
        "message": "Synthetic local provider signal for a small openable image.",
        "metadata": {
            "model": "deterministic-metadata-only",
            "backend": "python-standard-library",
            "width": width,
            "height": height,
            # Scanner-side metadata sanitization drops keys containing "image".
            "image_probe": "synthetic-value-should-not-appear-in-reports",
        },
    }


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


if __name__ == "__main__":
    raise SystemExit(main())
