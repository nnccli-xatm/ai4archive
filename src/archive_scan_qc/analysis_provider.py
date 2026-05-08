"""Offline analysis provider protocol for scan QC.

Providers run as local child processes and exchange JSONL only. The scanner
passes paths and run configuration, never image bytes, thumbnails, OCR text, or
file content.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import shlex
import subprocess
from typing import Any

from .rule_registry import validate_provider_rule_id
from .rules import VALID_SEVERITIES


class AnalysisProviderError(ValueError):
    """Raised when an offline provider cannot be run or returns invalid data."""


@dataclass(frozen=True)
class AnalysisProviderResult:
    findings: list[dict[str, Any]]
    metadata: dict[str, Any]


def run_analysis_provider(
    command: str,
    *,
    input_dir: Path,
    output_dir: Path,
    project_id: str,
    batch_id: str,
    files: list[dict[str, Any]],
    rules_profile: dict[str, Any],
) -> AnalysisProviderResult:
    if not command.strip():
        raise AnalysisProviderError("Analysis provider command must not be empty.")

    request_lines = [
        {
            "type": "image",
            "schema_version": "scan-qc.analysis-provider.input.v1",
            "project_id": project_id,
            "batch_id": batch_id,
            "input_dir": str(input_dir),
            "output_dir": str(output_dir),
            "relative_path": item["relative_path"],
            "path": str((input_dir / item["relative_path"]).resolve()),
            "openable": item.get("openable"),
            "extension": item.get("extension"),
            "format": item.get("format"),
            "width": item.get("width"),
            "height": item.get("height"),
            "dpi_x": item.get("dpi_x"),
            "dpi_y": item.get("dpi_y"),
            "color_mode": item.get("color_mode"),
            "rules_profile": rules_profile,
        }
        for item in files
    ]
    stdin = "\n".join(json.dumps(line, ensure_ascii=False) for line in request_lines) + ("\n" if request_lines else "")

    try:
        completed = subprocess.run(
            shlex.split(command),
            input=stdin,
            text=True,
            capture_output=True,
            check=False,
            timeout=300,
        )
    except FileNotFoundError as exc:
        raise AnalysisProviderError(f"Analysis provider command was not found: {exc.filename}") from exc
    except subprocess.TimeoutExpired as exc:
        raise AnalysisProviderError("Analysis provider command timed out after 300 seconds.") from exc

    if completed.returncode != 0:
        detail = _trim(completed.stderr.strip() or completed.stdout.strip())
        suffix = f": {detail}" if detail else "."
        raise AnalysisProviderError(f"Analysis provider exited with code {completed.returncode}{suffix}")

    return _parse_provider_output(completed.stdout, {item["relative_path"] for item in files})


def _parse_provider_output(stdout: str, valid_paths: set[str]) -> AnalysisProviderResult:
    findings: list[dict[str, Any]] = []
    metadata: dict[str, Any] = {
        "schema_version": "scan-qc.analysis-provider.output.v1",
        "transport": "local-process-jsonl-stdin-stdout",
        "findings_accepted": 0,
        "metadata_records": 0,
        "provider": {},
    }

    for line_number, raw_line in enumerate(stdout.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AnalysisProviderError(
                f"Analysis provider output line {line_number} is not valid JSON: {exc.msg}"
            ) from exc
        if not isinstance(payload, dict):
            raise AnalysisProviderError(f"Analysis provider output line {line_number} must be a JSON object.")

        record_type = payload.get("type", "finding")
        if record_type == "metadata":
            provider_metadata = payload.get("provider", {})
            if not isinstance(provider_metadata, dict):
                raise AnalysisProviderError(
                    f"Analysis provider metadata line {line_number} field 'provider' must be an object."
                )
            metadata["metadata_records"] += 1
            metadata["provider"].update(_safe_metadata(provider_metadata))
            continue
        if record_type != "finding":
            raise AnalysisProviderError(
                f"Analysis provider output line {line_number} field 'type' must be 'finding' or 'metadata'."
            )
        findings.append(_validated_finding(payload, line_number, valid_paths))

    metadata["findings_accepted"] = len(findings)
    return AnalysisProviderResult(findings=findings, metadata=metadata)


def _validated_finding(payload: dict[str, Any], line_number: int, valid_paths: set[str]) -> dict[str, Any]:
    relative_path = _required_string(payload, "relative_path", line_number)
    if relative_path not in valid_paths:
        raise AnalysisProviderError(
            f"Analysis provider output line {line_number} references unknown relative_path '{relative_path}'."
        )
    rule = _required_string(payload, "rule", line_number)
    try:
        validate_provider_rule_id(rule)
    except ValueError as exc:
        raise AnalysisProviderError(f"Analysis provider output line {line_number}: {exc}") from exc
    severity = _required_string(payload, "severity", line_number)
    if severity not in VALID_SEVERITIES:
        raise AnalysisProviderError(
            f"Analysis provider output line {line_number} field 'severity' must be one of P0, P1, or P2."
        )
    confidence = payload.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= float(confidence) <= 1:
        raise AnalysisProviderError(
            f"Analysis provider output line {line_number} field 'confidence' must be a number between 0 and 1."
        )
    message = _required_string(payload, "message", line_number)
    metadata = payload.get("metadata", {})
    if not isinstance(metadata, dict):
        raise AnalysisProviderError(f"Analysis provider output line {line_number} field 'metadata' must be an object.")
    return {
        "relative_path": relative_path,
        "rule": rule,
        "severity": severity,
        "confidence": round(float(confidence), 6),
        "message": message,
        "source": "provider",
        "metadata": _safe_metadata(metadata),
    }


def _required_string(payload: dict[str, Any], field: str, line_number: int) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise AnalysisProviderError(
            f"Analysis provider output line {line_number} field '{field}' must be a non-empty string."
        )
    return value


def _safe_metadata(raw: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    blocked_tokens = ("ocr", "text", "content", "thumbnail", "image", "bytes", "path", "filename", "sha")
    for key, value in raw.items():
        if not isinstance(key, str) or not key:
            continue
        if any(token in key.lower() for token in blocked_tokens):
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            safe[key] = value
        elif isinstance(value, list):
            safe[key] = [item for item in value if isinstance(item, (str, int, float, bool)) or item is None]
        elif isinstance(value, dict):
            safe[key] = _safe_metadata(value)
    return safe


def _trim(value: str, limit: int = 500) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."
