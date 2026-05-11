"""Public-safe artifact readiness checklist generator."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from .evidence_bundle import _privacy_failures
from .workbench_summary import (
    KNOWN_WORKBENCH_ARTIFACTS,
    WorkbenchArtifact,
    _blocking_codes,
    _counts_by_code,
    _normalized_status,
    _ready_signal,
    _unsupported_code,
    _warning_codes,
)


ARTIFACT_READINESS_JSON = "artifact_readiness_checklist.json"
PUBLIC_SAFE_ARTIFACT_READINESS_JSON = "public_safe_artifact_readiness.json"
SCHEMA_VERSION = "scan-qc-artifact-readiness-checklist.v1"

_PASS_STATUSES = {"pass", "passed", "ok", "success", "no_candidates", "no_inputs"}
_FAIL_STATUSES = {"fail", "failed", "error", "blocked"}
_REQUIRED_ARTIFACTS = {
    "aggregate_evidence_bundle_summary.json",
    "final_production_handoff_summary.json",
    "public_safe_validation_index.json",
    "workbench_public_summary.json",
}
_READINESS_ARTIFACTS = (
    WorkbenchArtifact(
        "workbench_public_summary.json",
        "workbench_public_summary",
        "Workbench public summary",
        "scan-qc.workbench-public-summary.",
    ),
    *KNOWN_WORKBENCH_ARTIFACTS,
)


def write_artifact_readiness_checklist(
    *,
    evidence_dir: Path | None = None,
    files: list[Path] | None = None,
    output_path: Path | None = None,
    output_name: str = ARTIFACT_READINESS_JSON,
) -> tuple[Path, dict[str, Any]]:
    checklist = build_artifact_readiness_checklist(evidence_dir=evidence_dir, files=files)
    path = output_path or ((evidence_dir or Path.cwd()) / output_name)
    if path.suffix == "":
        path = path / output_name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(checklist, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path, checklist


def build_artifact_readiness_checklist(
    *,
    evidence_dir: Path | None = None,
    files: list[Path] | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    if evidence_dir is None and not files:
        raise ValueError("provide --evidence-dir or at least one --file aggregate JSON input.")
    if evidence_dir is not None and files:
        raise ValueError("use either --evidence-dir or --file inputs, not both.")

    explicit_mode = bool(files)
    paths_by_name, rejected_inputs = _paths_by_name(evidence_dir=evidence_dir, files=files or [])
    rows: dict[str, dict[str, Any]] = {}
    blocking_items: list[dict[str, str]] = []
    warning_items: list[dict[str, str]] = []
    privacy_failure_count = 0

    for artifact in _READINESS_ARTIFACTS:
        if artifact.name in {ARTIFACT_READINESS_JSON, PUBLIC_SAFE_ARTIFACT_READINESS_JSON}:
            continue
        row, blockers, warnings = _readiness_row(
            paths_by_name.get(artifact.name),
            artifact,
            explicit_mode=explicit_mode,
        )
        rows[artifact.name] = row
        blocking_items.extend(blockers)
        warning_items.extend(warnings)
        privacy_failure_count += len(row["privacy_failure_codes"])

    for _name, code in rejected_inputs:
        blocking_items.append({"artifact": "unsupported_input", "category": "unsupported", "code": code})

    required_missing = sum(1 for row in rows.values() if row["required"] and not row["present"])
    optional_missing = sum(1 for row in rows.values() if not row["required"] and not row["present"])
    present_count = sum(1 for row in rows.values() if row["present"])
    failed_count = sum(1 for row in rows.values() if row["present"] and row["status"] == "fail")
    passed_count = sum(1 for row in rows.values() if row["present"] and row["status"] == "pass")
    blocking_counts_by_code = _counts_by_code(blocking_items)
    warning_counts_by_code = _counts_by_code(warning_items)
    ready = required_missing == 0 and failed_count == 0 and not blocking_items and privacy_failure_count == 0
    status = "pass" if ready else "fail"
    timestamp = generated_at or datetime.now(timezone.utc).isoformat()

    summary = {
        "known_artifacts": len(rows),
        "artifacts_present": present_count,
        "artifacts_passed": passed_count,
        "artifacts_failed": failed_count,
        "required_missing_count": required_missing,
        "optional_missing_count": optional_missing,
        "missing_count": required_missing + optional_missing,
        "unsupported_inputs": len(rejected_inputs),
        "blocking_count": len(blocking_items),
        "warning_count": len(warning_items),
        "privacy_blocker_count": privacy_failure_count,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": timestamp,
        "status": status,
        "ready": ready,
        "summary": summary,
        "aggregate_counts": summary,
        "blocking_counts_by_code": blocking_counts_by_code,
        "warning_counts_by_code": warning_counts_by_code,
        "blocking_items": blocking_items,
        "warning_items": warning_items,
        "artifact_readiness_checklist": rows,
        "public_safe_artifact_readiness": rows,
        "privacy": {
            "status": "pass" if privacy_failure_count == 0 and not rejected_inputs else "fail",
            "aggregate_only": privacy_failure_count == 0 and not rejected_inputs,
            "private_indicators_found": privacy_failure_count > 0,
            "private_indicator_count": privacy_failure_count,
            "unsupported_private_input_count": len(rejected_inputs),
            "contains_paths": False,
            "contains_filenames": False,
            "contains_hashes": False,
            "contains_ocr_text": False,
            "contains_thumbnails": False,
            "contains_image_content": False,
            "contains_secrets": False,
            "contains_row_level_findings": False,
            "contains_manifests": False,
            "contains_derivative_references": False,
            "contains_reviewer_notes": False,
            "contains_provider_command_strings": False,
            "contains_prompts": False,
            "contains_raw_model_output": False,
            "contains_embeddings": False,
            "contains_environment_values": False,
            "contains_network_locations": False,
            "redacts_private_values": True,
        },
        "sensitive_values_omitted": True,
    }


def _paths_by_name(*, evidence_dir: Path | None, files: list[Path]) -> tuple[dict[str, Path], list[tuple[str, str]]]:
    if evidence_dir is not None:
        names = {artifact.name for artifact in _READINESS_ARTIFACTS}
        names.discard(ARTIFACT_READINESS_JSON)
        names.discard(PUBLIC_SAFE_ARTIFACT_READINESS_JSON)
        return ({name: evidence_dir / name for name in names}, [])
    result: dict[str, Path] = {}
    rejected: list[tuple[str, str]] = []
    known = {artifact.name for artifact in _READINESS_ARTIFACTS}
    for path in files:
        name = path.name
        if name in result:
            raise ValueError(f"duplicate aggregate JSON input: {name}")
        if name in known and name not in {ARTIFACT_READINESS_JSON, PUBLIC_SAFE_ARTIFACT_READINESS_JSON}:
            result[name] = path
        else:
            rejected.append((name, _unsupported_code(name)))
    return result, rejected


def _readiness_row(
    path: Path | None,
    artifact: WorkbenchArtifact,
    *,
    explicit_mode: bool,
) -> tuple[dict[str, Any], list[dict[str, str]], list[dict[str, str]]]:
    required = artifact.name in _REQUIRED_ARTIFACTS
    base = {
        "artifact": artifact.name,
        "name": artifact.name,
        "type": artifact.category,
        "category": artifact.category,
        "label": artifact.label,
        "present": False,
        "required": required,
        "status": "not_provided" if explicit_mode else ("missing" if required else "optional_missing"),
        "reported_status": None,
        "ready": None,
        "pass": None,
        "generated_at": None,
        "blocking_count": 0,
        "warning_count": 0,
        "blocking_counts_by_code": {},
        "warning_counts_by_code": {},
        "privacy_status": "not_checked",
        "privacy_failure_codes": [],
    }
    if path is None or not path.exists():
        if required and not explicit_mode:
            blocker = _blocker(artifact, "required_aggregate_artifact_missing")
            base["blocking_count"] = 1
            base["blocking_counts_by_code"] = {"required_aggregate_artifact_missing": 1}
            return base, [blocker], []
        return base, [], []
    if not path.is_file():
        base.update({"present": True, "status": "fail", "blocking_count": 1})
        blocker = _blocker(artifact, "aggregate_input_not_file")
        base["blocking_counts_by_code"] = {"aggregate_input_not_file": 1}
        return base, [blocker], []

    raw = path.read_text(encoding="utf-8")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        base.update({"present": True, "status": "fail", "blocking_count": 1})
        blocker = _blocker(artifact, "malformed_json")
        base["blocking_counts_by_code"] = {"malformed_json": 1}
        return base, [blocker], []
    if not isinstance(payload, dict):
        base.update({"present": True, "status": "fail", "blocking_count": 1})
        blocker = _blocker(artifact, "json_root_not_object")
        base["blocking_counts_by_code"] = {"json_root_not_object": 1}
        return base, [blocker], []

    blockers: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    schema = payload.get("schema_version")
    if artifact.schema_prefix and (not isinstance(schema, str) or not schema.startswith(artifact.schema_prefix)):
        blockers.append(_blocker(artifact, "schema_version_unexpected"))
    reported_status = _normalized_status(payload)
    if reported_status in _FAIL_STATUSES:
        blockers.append(_blocker(artifact, "artifact_status_failed"))
    elif reported_status not in _PASS_STATUSES:
        blockers.append(_blocker(artifact, "artifact_status_unknown"))
    privacy_codes = sorted(_privacy_failures(payload, raw))
    blockers.extend(_blocker(artifact, code) for code in privacy_codes)
    blockers.extend(_blocker(artifact, code) for code in _blocking_codes(payload))
    warnings.extend(_warning(artifact, code) for code in _warning_codes(payload))
    ready = _ready_signal(payload)

    base.update(
        {
            "present": True,
            "status": "pass" if not blockers else "fail",
            "reported_status": reported_status,
            "ready": ready,
            "pass": ready if ready is not None else (not blockers if reported_status in _PASS_STATUSES else None),
            "generated_at": payload.get("generated_at") if isinstance(payload.get("generated_at"), str) else None,
            "blocking_count": len(blockers),
            "warning_count": len(warnings),
            "blocking_counts_by_code": _counts_by_code(blockers),
            "warning_counts_by_code": _counts_by_code(warnings),
            "privacy_status": "pass" if not privacy_codes else "fail",
            "privacy_failure_codes": privacy_codes,
        }
    )
    return base, blockers, warnings


def _blocker(artifact: WorkbenchArtifact, code: str) -> dict[str, str]:
    return {"artifact": artifact.name, "category": artifact.category, "code": code}


def _warning(artifact: WorkbenchArtifact, code: str) -> dict[str, str]:
    return {"artifact": artifact.name, "category": artifact.category, "code": code}
