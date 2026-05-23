"""Final aggregate production handoff summary."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from .evidence_bundle import EVIDENCE_BUNDLE_JSON, _privacy_failures


FINAL_HANDOFF_JSON = "final_production_handoff_summary.json"
RELEASE_CANDIDATE_JSON = "release_candidate_summary.json"
DEEP_INSPECTION_CANDIDATE_JSON = "deep_inspection_candidate_summary.json"
REVIEW_DECISION_VERIFICATION_JSON = "review_decision_verification_summary.json"
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
    deep_inspection_candidate = _load_aggregate_artifact(evidence_dir / DEEP_INSPECTION_CANDIDATE_JSON, required=False)
    review_decision_verification = _load_aggregate_artifact(evidence_dir / REVIEW_DECISION_VERIFICATION_JSON, required=False)

    artifacts = {
        EVIDENCE_BUNDLE_JSON: _artifact_summary(evidence),
        RELEASE_CANDIDATE_JSON: _artifact_summary(release_candidate),
        DEEP_INSPECTION_CANDIDATE_JSON: _artifact_summary(deep_inspection_candidate),
        REVIEW_DECISION_VERIFICATION_JSON: _artifact_summary(review_decision_verification),
    }
    blockers = _blocking_items(evidence, release_candidate, deep_inspection_candidate, review_decision_verification)
    checks_passed = (
        _sum_int(evidence.payload, "checks_passed")
        + _release_candidate_check_passed(release_candidate)
        + _deep_inspection_candidate_check_passed(deep_inspection_candidate)
    )
    checks_failed = len(blockers)
    blocking_item_count = len(blockers)
    status = "pass" if blocking_item_count == 0 else "fail"
    handoff_blockers = _handoff_blocker_summary_zh(
        status=status,
        release_candidate=release_candidate,
        review_decision_verification=review_decision_verification,
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "status": status,
        "ready_for_handoff": status == "pass",
        "handoff_status_zh": handoff_blockers["status_zh"],
        "admin_summary_zh": handoff_blockers["summary_zh"],
        "handoff_blocker_summary_zh": handoff_blockers,
        "checks_passed": checks_passed,
        "checks_failed": checks_failed,
        "blocking_item_count": blocking_item_count,
        "blocking_items": blockers,
        "artifact_status_summary": artifacts,
        "privacy": {
            "aggregate_only": status == "pass",
            "source_inputs": [EVIDENCE_BUNDLE_JSON, RELEASE_CANDIDATE_JSON, DEEP_INSPECTION_CANDIDATE_JSON, REVIEW_DECISION_VERIFICATION_JSON],
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
    expected_schema_prefix = _expected_schema_prefix(name)
    if expected_schema_prefix:
        schema = payload.get("schema_version")
        if not isinstance(schema, str) or not schema.startswith(expected_schema_prefix):
            blockers.append(_blocker(name, "schema_version_unexpected"))
    for code in _privacy_failures(payload, raw):
        blockers.append(_blocker(name, code))
    reported_status = _normalized_status(payload)
    if name == DEEP_INSPECTION_CANDIDATE_JSON and reported_status in {"no_candidates", "no_inputs"}:
        pass
    elif reported_status in _FAIL_STATUSES:
        blockers.append(_blocker(name, "aggregate_status_failed"))
    elif reported_status not in _PASS_STATUSES:
        blockers.append(_blocker(name, "aggregate_status_unknown"))
    if name == EVIDENCE_BUNDLE_JSON:
        for item in _aggregate_blocking_items(payload):
            blockers.append(_blocker(name, item))
    if name == RELEASE_CANDIDATE_JSON and payload.get("ready_for_release_candidate") is False:
        blockers.append(_blocker(name, "release_candidate_not_ready"))
    if name == DEEP_INSPECTION_CANDIDATE_JSON:
        blockers.extend(_blocker(name, code) for code in _deep_inspection_candidate_blocker_codes(payload))
    if name == REVIEW_DECISION_VERIFICATION_JSON:
        blockers.extend(_blocker(name, code) for code in _review_decision_verification_blocker_codes(payload))

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
    summary = {
        "present": artifact.present,
        "required": artifact.required,
        "status": artifact.status if artifact.present else ("missing" if artifact.required else "optional_missing"),
        "reported_status": _normalized_status(payload) if payload else None,
        "checks_passed": _sum_int(payload, "checks_passed") if payload else 0,
        "checks_failed": _sum_int(payload, "checks_failed") if payload else 0,
        "blocking_item_count": len(artifact.blockers),
    }
    if artifact.name == DEEP_INSPECTION_CANDIDATE_JSON and payload:
        summary.update(
            {
                "candidate_total": _safe_int(payload.get("candidate_total")),
                "candidates_by_reason": _safe_count_map(payload.get("candidates_by_reason")),
                "candidates_by_severity": _safe_count_map(payload.get("candidates_by_severity")),
                "provider_configured": payload.get("provider_configured") if isinstance(payload.get("provider_configured"), bool) else None,
                "provider_count": _safe_int(payload.get("provider_count")),
                "checks_passed": _candidate_check_count(payload.get("checks_passed")),
                "checks_failed": _candidate_check_count(payload.get("checks_failed")),
                "no_inference_run": payload.get("no_inference_run") is True,
            }
        )
    if artifact.name == REVIEW_DECISION_VERIFICATION_JSON and payload:
        summary.update(
            {
                "decision_summary": _review_decision_summary(payload.get("decision_summary")),
                "blocking_counts_by_code": _safe_count_map(payload.get("blocking_counts_by_code")),
                "warning_counts_by_code": _safe_count_map(payload.get("warning_counts_by_code")),
                "blocking_count": _safe_int(payload.get("blocking_count")),
                "warning_count": _safe_int(payload.get("warning_count")),
                "privacy_status": _review_decision_privacy_status(payload),
            }
        )
    return summary


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


def _deep_inspection_candidate_check_passed(artifact: _LoadedArtifact) -> int:
    if not artifact.present or artifact.payload is None or artifact.blockers:
        return 0
    return 1


def _sum_int(payload: dict[str, Any] | None, key: str) -> int:
    if not payload:
        return 0
    value = payload.get(key)
    if isinstance(value, int):
        return value
    if isinstance(value, list) and key in {"checks_passed", "checks_failed"}:
        return len(value)
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


def _expected_schema_prefix(name: str) -> str | None:
    if name == DEEP_INSPECTION_CANDIDATE_JSON:
        return "scan-qc.deep-inspection-candidates."
    if name == REVIEW_DECISION_VERIFICATION_JSON:
        return "scan-qc.review-decision-verification-summary."
    return None


def _deep_inspection_candidate_blocker_codes(payload: dict[str, Any]) -> list[str]:
    codes: list[str] = []
    checks_failed = payload.get("checks_failed")
    if not isinstance(checks_failed, list):
        codes.append("checks_failed_not_list")
    elif checks_failed:
        codes.append("checks_failed_present")
    if payload.get("privacy_status") != "aggregate_public_safe":
        codes.append("privacy_status_not_aggregate_public_safe")
    if payload.get("no_inference_run") is not True:
        codes.append("inference_run_not_allowed")
    return sorted(set(codes))


def _review_decision_verification_blocker_codes(payload: dict[str, Any]) -> list[str]:
    codes: list[str] = []
    checks_failed = payload.get("checks_failed")
    if not isinstance(checks_failed, int) or isinstance(checks_failed, bool) or checks_failed < 0:
        codes.append("checks_failed_not_integer")
    elif checks_failed > 0:
        codes.append("checks_failed_present")
    blocking_count = payload.get("blocking_count")
    if not isinstance(blocking_count, int) or isinstance(blocking_count, bool) or blocking_count < 0:
        codes.append("blocking_count_not_integer")
    elif blocking_count > 0:
        codes.append("review_decision_blocking_count_present")
    if not _safe_count_map(payload.get("blocking_counts_by_code")) and payload.get("blocking_counts_by_code") != {}:
        codes.append("blocking_counts_by_code_invalid")
    if _review_decision_privacy_status(payload) != "pass":
        codes.append("review_decision_privacy_not_public_safe")
    if not _review_decision_summary(payload.get("decision_summary")):
        codes.append("decision_summary_invalid")
    return sorted(set(codes))


def _review_decision_summary(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    completion_status = value.get("completion_status")
    if not isinstance(completion_status, str):
        return {}
    return {
        "total_decisions": _safe_int(value.get("total_decisions")),
        "pending": _safe_int(value.get("pending")),
        "accepted": _safe_int(value.get("accepted")),
        "rejected": _safe_int(value.get("rejected")),
        "rework": _safe_int(value.get("rework")),
        "completion_status": completion_status,
        "decision_counts": _safe_count_map(value.get("decision_counts")),
        "closure_gate_summary": _closure_gate_status(value.get("closure_gate_summary")),
    }


def _handoff_blocker_summary_zh(
    *,
    status: str,
    release_candidate: _LoadedArtifact,
    review_decision_verification: _LoadedArtifact,
) -> dict[str, Any]:
    release_payload = release_candidate.payload or {}
    release_digest = release_payload.get("acceptance_blocker_summary_zh")
    release_blockers = release_digest.get("blockers_zh") if isinstance(release_digest, dict) else None
    blockers = [item for item in release_blockers if isinstance(item, str)] if isinstance(release_blockers, list) else []
    closure = _release_closure_gate_status(release_payload)
    review_closure = _review_decision_closure_gate_status(review_decision_verification.payload)
    closure = _prefer_closure(closure, review_closure)
    if not blockers:
        open_p0 = closure["open_p0_count"]
        open_p1 = closure["open_p1_count"]
        handled_count = closure["manually_handled_count"]
        if open_p0 > 0 or open_p1 > 0:
            blockers.append(f"P0/P1 未关闭：未关闭 P0 {open_p0} 项，未关闭 P1 {open_p1} 项。")
        if closure["can_complete_delivery"] is False and handled_count is None:
            blockers.append("人工处理结论不足：未提供可确认的人工处理闭环汇总。")
        elif closure["can_complete_delivery"] is False and open_p0 == 0 and open_p1 == 0:
            blockers.append(f"人工处理结论不足：已有人工处理结论 {handled_count} 项，但闭环状态仍未达到交接条件。")
    sampling = _release_acceptance_sampling_status(release_payload)
    cleanup_warning_digest = _release_cleanup_quality_warning_digest_zh(release_payload)
    if sampling["provided"]:
        target = sampling["target_sample_count"] or 0
        generated = sampling["generated_sample_task_count"] or 0
        reviewed = sampling["reviewed_sample_count"] or 0
        if sampling["sample_task_target_met"] is False and not any("抽检任务未达到目标比例" in item for item in blockers):
            blockers.append(f"抽检任务未达到目标比例：目标 {target} 项，已生成 {generated} 项。")
        if sampling["sampling_target_met"] is False and not any("抽检复核未达到目标比例" in item for item in blockers):
            blockers.append(f"抽检复核未达到目标比例：目标 {target} 项，已复核 {reviewed} 项。")
    ready = status == "pass"
    if not blockers and not ready:
        blockers.append("交接聚合状态未通过，请查看阻塞代码计数后重新生成交接摘要。")
    status_zh = "可交接" if ready and not blockers else "不可交接"
    summary_zh = (
        "可交接：P0/P1 已关闭，人工处理结论和抽检比例聚合检查均已通过。"
        if status_zh == "可交接"
        else "不可交接：" + "；".join(blockers)
    )
    return {
        "status_zh": status_zh,
        "can_handoff": status_zh == "可交接",
        "summary_zh": summary_zh,
        "blockers_zh": blockers,
        "cleanup_quality_warnings_zh": cleanup_warning_digest,
        "cleanup_quality_warning_codes": [item["code"] for item in cleanup_warning_digest],
        "closure_gate_summary": closure,
        "acceptance_sampling": sampling,
        "reused_aggregate_fields": [
            "closure_gate_summary",
            "acceptance_sampling",
            "release_candidate_summary",
            "review_decision_verification_summary",
            "blocking_items",
            "warning_items",
        ],
    }


def _release_closure_gate_status(payload: dict[str, Any]) -> dict[str, Any]:
    production = payload.get("production_validation")
    closure = production.get("closure_gate_summary") if isinstance(production, dict) else None
    if not isinstance(closure, dict):
        digest = payload.get("acceptance_blocker_summary_zh")
        closure = digest.get("closure_gate_summary") if isinstance(digest, dict) else None
    return _closure_gate_status(closure)


def _review_decision_closure_gate_status(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return _closure_gate_status(None)
    decision = payload.get("decision_summary")
    closure = decision.get("closure_gate_summary") if isinstance(decision, dict) else None
    return _closure_gate_status(closure)


def _prefer_closure(primary: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    if primary["can_complete_delivery"] is not None or primary["open_p0_count"] or primary["open_p1_count"]:
        return primary
    if fallback["can_complete_delivery"] is not None or fallback["open_p0_count"] or fallback["open_p1_count"]:
        return fallback
    return primary


def _release_acceptance_sampling_status(payload: dict[str, Any]) -> dict[str, Any]:
    production = payload.get("production_validation")
    sampling = production.get("acceptance_sampling") if isinstance(production, dict) else None
    if not isinstance(sampling, dict):
        digest = payload.get("acceptance_blocker_summary_zh")
        sampling = digest.get("acceptance_sampling") if isinstance(digest, dict) else None
    if not isinstance(sampling, dict) or sampling.get("provided") is not True:
        return {
            "provided": False,
            "target_sample_count": None,
            "generated_sample_task_count": None,
            "reviewed_sample_count": None,
            "sample_task_target_met": None,
            "sampling_target_met": None,
        }
    return {
        "provided": True,
        "target_sample_count": _safe_optional_int(sampling.get("target_sample_count")),
        "generated_sample_task_count": _safe_optional_int(sampling.get("generated_sample_task_count")),
        "reviewed_sample_count": _safe_optional_int(sampling.get("reviewed_sample_count")),
        "sample_task_target_met": sampling.get("sample_task_target_met") if isinstance(sampling.get("sample_task_target_met"), bool) else None,
        "sampling_target_met": sampling.get("sampling_target_met") if isinstance(sampling.get("sampling_target_met"), bool) else None,
    }


def _release_cleanup_quality_warning_digest_zh(payload: dict[str, Any]) -> list[dict[str, str]]:
    warning_items = _release_acceptance_warning_items(payload)
    digests: list[dict[str, str]] = []
    seen_codes: set[str] = set()
    for item in warning_items:
        code = item.get("code")
        title_zh = item.get("title_zh")
        message_zh = item.get("message_zh")
        next_step_zh = item.get("next_step_zh")
        if not isinstance(code, str) or not code:
            continue
        if code in seen_codes:
            continue
        if not all(isinstance(value, str) and value for value in (title_zh, message_zh, next_step_zh)):
            continue
        seen_codes.add(code)
        digests.append(
            {
                "code": code,
                "title_zh": title_zh,
                "message_zh": message_zh,
                "next_step_zh": next_step_zh,
            }
        )
    return digests


def _release_acceptance_warning_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[Any] = []
    candidates.append(payload.get("warning_items"))

    acceptance = payload.get("acceptance")
    if isinstance(acceptance, dict):
        candidates.append(acceptance.get("warning_items"))
        summary = acceptance.get("summary")
        if isinstance(summary, dict):
            candidates.append(summary.get("warning_items"))

    production = payload.get("production_validation")
    if isinstance(production, dict):
        acceptance_summary = production.get("acceptance_summary")
        if isinstance(acceptance_summary, dict):
            candidates.append(acceptance_summary.get("warning_items"))

    for candidate in candidates:
        if isinstance(candidate, list):
            return [item for item in candidate if isinstance(item, dict)]
    return []


def _closure_gate_status(value: Any) -> dict[str, Any]:
    closure = value if isinstance(value, dict) else {}
    can_complete = closure.get("can_complete_delivery")
    return {
        "open_p0_count": _safe_optional_int(closure.get("open_p0_count")) or 0,
        "open_p1_count": _safe_optional_int(closure.get("open_p1_count")) or 0,
        "manually_handled_count": _safe_optional_int(closure.get("manually_handled_count")),
        "can_complete_delivery": can_complete if isinstance(can_complete, bool) else None,
    }


def _review_decision_privacy_status(payload: dict[str, Any]) -> str | None:
    privacy = payload.get("privacy")
    if isinstance(privacy, dict) and isinstance(privacy.get("status"), str):
        return privacy["status"]
    return None


def _safe_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _safe_optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _safe_count_map(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): count
        for key, count in sorted(value.items())
        if isinstance(key, str) and isinstance(count, int) and not isinstance(count, bool) and count >= 0
    }


def _candidate_check_count(value: Any) -> int:
    return len(value) if isinstance(value, list) else 0
