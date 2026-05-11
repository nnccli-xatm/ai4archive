"""Final aggregate production handoff summary."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from .evidence_bundle import EVIDENCE_BUNDLE_JSON, _privacy_failures


FINAL_HANDOFF_JSON = "final_production_handoff_summary.json"
RELEASE_CANDIDATE_JSON = "release_candidate_summary.json"
SCHEMA_VERSION = "scan-qc.final-production-handoff-summary.v1"

_PASS_STATUSES = {"pass", "passed", "ok", "success"}
_FAIL_STATUSES = {"fail", "failed", "error", "blocked"}


def write_final_handoff_summary(evidence_dir: Path, output_path: Path | None = None) -> tuple[Path, dict[str, Any]]:
    summary = build_final_handoff_summary(evidence_dir)
    path = output_path or evidence_dir / FINAL_HANDOFF_JSON
    if path.suffix == "":
        path = path / FINAL_HANDOFF_JSON
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path, summary


def build_final_handoff_summary(evidence_dir: Path, *, generated_at: str | None = None) -> dict[str, Any]:
    evidence = _load_aggregate_artifact(evidence_dir / EVIDENCE_BUNDLE_JSON, required=True)
    release_candidate = _load_aggregate_artifact(evidence_dir / RELEASE_CANDIDATE_JSON, required=False)

    artifacts = {
        EVIDENCE_BUNDLE_JSON: _artifact_summary(evidence),
        RELEASE_CANDIDATE_JSON: _artifact_summary(release_candidate),
    }
    blockers = _blocking_items(evidence, release_candidate)
    checks_passed = _sum_int(evidence.payload, "checks_passed") + _release_candidate_check_passed(release_candidate)
    checks_failed = len(blockers)
    blocking_item_count = len(blockers)
    status = "pass" if blocking_item_count == 0 else "fail"

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "status": status,
        "ready_for_handoff": status == "pass",
        "checks_passed": checks_passed,
        "checks_failed": checks_failed,
        "blocking_item_count": blocking_item_count,
        "blocking_items": blockers,
        "artifact_status_summary": artifacts,
        "privacy": {
            "aggregate_only": status == "pass",
            "source_inputs": [EVIDENCE_BUNDLE_JSON, RELEASE_CANDIDATE_JSON],
            "private_indicators_found": any(item["code"].startswith("private_") or item["code"].startswith("privacy_") for item in blockers),
            "private_indicator_count": sum(
                1 for item in blockers if item["code"].startswith("private_") or item["code"].startswith("privacy_")
            ),
            "contains_paths": False,
            "contains_filenames": False,
            "contains_hashes": False,
            "contains_ocr_text": False,
            "contains_thumbnails": False,
            "contains_image_content": False,
            "contains_secrets": False,
            "contains_row_level_findings": False,
            "redacts_private_values": True,
        },
        "sensitive_values_omitted": True,
    }


class _LoadedArtifact:
    def __init__(
        self,
        *,
        name: str,
        required: bool,
        present: bool,
        status: str,
        payload: dict[str, Any] | None = None,
        blockers: list[dict[str, str]] | None = None,
    ) -> None:
        self.name = name
        self.required = required
        self.present = present
        self.status = status
        self.payload = payload
        self.blockers = blockers or []


def _load_aggregate_artifact(path: Path, *, required: bool) -> _LoadedArtifact:
    name = path.name
    if not path.exists():
        blockers = [_blocker(name, "required_aggregate_input_missing")] if required else []
        return _LoadedArtifact(name=name, required=required, present=False, status="missing", blockers=blockers)
    if not path.is_file():
        return _LoadedArtifact(
            name=name,
            required=required,
            present=True,
            status="invalid",
            blockers=[_blocker(name, "aggregate_input_not_file")],
        )

    raw = path.read_text(encoding="utf-8")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return _LoadedArtifact(
            name=name,
            required=required,
            present=True,
            status="invalid_json",
            blockers=[_blocker(name, "malformed_json")],
        )
    if not isinstance(payload, dict):
        return _LoadedArtifact(
            name=name,
            required=required,
            present=True,
            status="invalid_schema",
            blockers=[_blocker(name, "json_root_not_object")],
        )

    blockers: list[dict[str, str]] = []
    for code in _privacy_failures(payload, raw):
        blockers.append(_blocker(name, code))
    reported_status = _normalized_status(payload)
    if reported_status in _FAIL_STATUSES:
        blockers.append(_blocker(name, "aggregate_status_failed"))
    elif reported_status not in _PASS_STATUSES:
        blockers.append(_blocker(name, "aggregate_status_unknown"))
    if name == EVIDENCE_BUNDLE_JSON:
        for item in _aggregate_blocking_items(payload):
            blockers.append(_blocker(name, item))
    if name == RELEASE_CANDIDATE_JSON and payload.get("ready_for_release_candidate") is False:
        blockers.append(_blocker(name, "release_candidate_not_ready"))

    return _LoadedArtifact(
        name=name,
        required=required,
        present=True,
        status="pass" if not blockers else "fail",
        payload=payload,
        blockers=blockers,
    )


def _aggregate_blocking_items(payload: dict[str, Any]) -> list[str]:
    blocking_items = payload.get("blocking_items")
    if isinstance(blocking_items, list):
        count = len(blocking_items)
    else:
        count = _sum_int(payload, "blocking_item_count")
    if count > 0:
        return ["aggregate_evidence_blocking_items_present"]
    return []


def _artifact_summary(artifact: _LoadedArtifact) -> dict[str, Any]:
    payload = artifact.payload or {}
    return {
        "present": artifact.present,
        "required": artifact.required,
        "status": artifact.status if artifact.present else ("missing" if artifact.required else "optional_missing"),
        "reported_status": _normalized_status(payload) if payload else None,
        "checks_passed": _sum_int(payload, "checks_passed") if payload else 0,
        "checks_failed": _sum_int(payload, "checks_failed") if payload else 0,
        "blocking_item_count": len(artifact.blockers),
    }


def _blocking_items(*artifacts: _LoadedArtifact) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    result: list[dict[str, str]] = []
    for artifact in artifacts:
        for item in artifact.blockers:
            key = (item["artifact"], item["code"])
            if key not in seen:
                seen.add(key)
                result.append(item)
    return result


def _release_candidate_check_passed(artifact: _LoadedArtifact) -> int:
    if not artifact.present or artifact.payload is None or artifact.blockers:
        return 0
    return 1


def _sum_int(payload: dict[str, Any] | None, key: str) -> int:
    if not payload:
        return 0
    value = payload.get(key)
    if isinstance(value, int):
        return value
    if key == "blocking_item_count":
        blocking_items = payload.get("blocking_items")
        if isinstance(blocking_items, list):
            return len(blocking_items)
    return 0


def _normalized_status(payload: dict[str, Any]) -> str | None:
    status = payload.get("status")
    return str(status).lower() if status is not None else None


def _blocker(artifact: str, code: str) -> dict[str, str]:
    return {"artifact": artifact, "code": code}
