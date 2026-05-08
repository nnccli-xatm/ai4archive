"""Delivery handoff manifest writer for local evidence review."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Any


HANDOFF_JSON = "delivery_handoff_manifest.json"
HANDOFF_CSV = "delivery_handoff_manifest.csv"
SCHEMA_VERSION = "scan-qc.delivery-handoff-manifest.v1"

AGGREGATE_PUBLIC_SAFE = "aggregate_public_safe"
SENSITIVE_LOCAL_EVIDENCE = "sensitive_local_evidence"

_AGGREGATE_SCHEMA_PREFIXES = (
    "scan-qc.run-plan-summary.",
    "scan-qc.review-summary.",
    "scan-qc.processing.audit.",
    "scan-qc.benchmark.",
    "scan-qc.acceptance-summary.",
    "scan-qc.rules-calibration.",
)

_AGGREGATE_FILENAMES = {
    "run_plan_summary.json",
    "review_summary.json",
    "processing_audit_summary.json",
    "benchmark_results.json",
    "benchmark_results.csv",
    "acceptance_summary.json",
    "rules_calibration_summary.json",
}

_SENSITIVE_SCHEMA_PREFIXES = (
    "scan-qc.phase1.",
    "scan-qc.review-template.",
    "scan-qc.processing.",
    "scan-qc.processing.retry.",
    "scan-qc.processing-review.",
)

_SENSITIVE_FILENAMES = {
    "scan_qc_report.json",
    "scan_qc_report.csv",
    "scan_qc_report.html",
    "review_template.csv",
    "review_template.json",
    "processing_manifest.json",
    "processing_retry_manifest.json",
    "processing_review_package.json",
    "processing_review_package.html",
}


def write_delivery_handoff_manifest(
    artifacts: list[tuple[str, Path]],
    out_dir: Path,
) -> tuple[Path, Path, dict[str, Any]]:
    if not artifacts:
        raise ValueError("at least one evidence artifact is required")

    records = [_artifact_record(role, path) for role, path in artifacts]
    records.sort(key=lambda item: (str(item["role"]), str(item["path"])))
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "privacy": {
            "copies_source_images": False,
            "uploads": False,
            "classification_policy": (
                "Known aggregate summary artifacts are aggregate_public_safe. "
                "Known row-level reports, processing manifests, review packages, "
                "and unknown artifacts are sensitive_local_evidence."
            ),
        },
        "summary": {
            "artifact_count": len(records),
            "aggregate_public_safe_count": sum(1 for record in records if record["sensitivity"] == AGGREGATE_PUBLIC_SAFE),
            "sensitive_local_evidence_count": sum(
                1 for record in records if record["sensitivity"] == SENSITIVE_LOCAL_EVIDENCE
            ),
            "total_bytes": sum(int(record["size_bytes"]) for record in records),
        },
        "artifacts": records,
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / HANDOFF_JSON
    csv_path = out_dir / HANDOFF_CSV
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _write_csv(csv_path, records)
    return json_path, csv_path, payload


def _artifact_record(role: str, path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ValueError(f"missing artifact: {path}")
    if not path.is_file():
        raise ValueError(f"artifact is not a file: {path}")

    schema_version = _schema_version(path)
    sensitivity, reason = _classify(path, schema_version)
    return {
        "role": role,
        "path": str(path),
        "name": path.name,
        "size_bytes": path.stat().st_size,
        "sha256": _sha256(path),
        "schema_version": schema_version,
        "sensitivity": sensitivity,
        "classification_reason": reason,
    }


def _schema_version(path: Path) -> str | None:
    if path.suffix.lower() != ".json":
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    schema = payload.get("schema_version")
    return schema if isinstance(schema, str) and schema else None


def _classify(path: Path, schema_version: str | None) -> tuple[str, str]:
    name = path.name
    if name in _SENSITIVE_FILENAMES:
        return SENSITIVE_LOCAL_EVIDENCE, "known row-level or local-review artifact name"
    if schema_version and schema_version.startswith(_AGGREGATE_SCHEMA_PREFIXES):
        return AGGREGATE_PUBLIC_SAFE, "known aggregate summary schema"
    if name in _AGGREGATE_FILENAMES:
        return AGGREGATE_PUBLIC_SAFE, "known aggregate summary artifact name"
    if schema_version and schema_version.startswith(_SENSITIVE_SCHEMA_PREFIXES):
        return SENSITIVE_LOCAL_EVIDENCE, "known row-level or local-review schema"
    return SENSITIVE_LOCAL_EVIDENCE, "unknown artifact; classified conservatively"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_csv(path: Path, records: list[dict[str, Any]]) -> None:
    fieldnames = [
        "role",
        "path",
        "name",
        "size_bytes",
        "sha256",
        "schema_version",
        "sensitivity",
        "classification_reason",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(records)
