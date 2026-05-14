"""Sandbox-safe smoke validation for production workbench review decisions."""

from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from archive_scan_qc.local_workbench import REVIEW_DECISION_DRAFT_JSON, WorkbenchController  # noqa: E402
from archive_scan_qc.production_review_queue import PRODUCTION_REVIEW_QUEUE_JSON  # noqa: E402


WORKBENCH = ROOT / "docs" / "production-workbench-prototype.html"
PRIVATE_TERMS = {
    "/Users/",
    "/private/",
    "\\",
    ".jpg",
    ".jpeg",
    ".png",
    ".tif",
    ".tiff",
    "relative_path",
    "source_path",
    "thumbnail",
    "sha256",
    "hash",
    "OCR",
    "ocr_text",
    "preview_url",
    "original_path",
    "processed_path",
}
OPERATOR_DECISIONS = [
    ("pass", "false_positive", "确认通过"),
    ("rescan", "needs_rescan", "退回重扫"),
    ("reprocess", "fixed_externally", "重新处理图片"),
]


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _fake_queue() -> dict[str, Any]:
    return {
        "schema_version": "scan-qc.production-review-queue.v1",
        "generated_at": "2026-05-14T00:00:00+00:00",
        "privacy": {
            "local_only": True,
            "aggregate_only": False,
            "contains_hashes": False,
            "contains_thumbnails": False,
            "contains_image_bytes": False,
            "contains_base64": False,
            "contains_ocr_text": False,
            "contains_public_evidence": False,
        },
        "summary": {
            "total_items": 3,
            "items_by_severity": {"P0": 1, "P1": 1, "P2": 1, "P3": 0, "info": 0},
            "items_by_suggested_action": {"pass": 1, "rescan": 1, "reprocess": 1, "keep_original_trace": 0, "skip": 0},
            "ready_for_operator_review": True,
        },
        "items": [
            {
                "local_id": f"PRQ-SMOKE-{index:03d}",
                "relative_path": f"smoke-page-{index:03d}.png",
                "severity": severity,
                "source_category": "scan_qc",
                "source_ref": "aggregate_smoke",
                "reason_zh": reason,
                "focus_hints_zh": ["查看画面是否完整可读", "确认后选择处理决定"],
                "suggested_action": decision,
                "sensitivity": {
                    "local_only": True,
                    "contains_image_bytes": False,
                    "contains_thumbnail": False,
                    "contains_hash": False,
                    "contains_ocr_text": False,
                },
            }
            for index, (decision, severity, reason) in enumerate(
                [
                    ("pass", "P2", "图片可以继续使用，请确认通过。"),
                    ("rescan", "P0", "扫描质量需要人工确认，请退回重扫。"),
                    ("reprocess", "P1", "处理后图片需要重新生成，请重新处理图片。"),
                ],
                start=1,
            )
        ],
    }


def _decision_summary(local_ids: list[str]) -> dict[str, Any]:
    counts = {
        "pending": 0,
        "accepted_issue": 0,
        "false_positive": 0,
        "fixed_externally": 0,
        "needs_rescan": 0,
        "blocked": 0,
    }
    rows = []
    operator_rows = []
    for local_id, (operator_decision, contract_decision, _label) in zip(local_ids, OPERATOR_DECISIONS, strict=True):
        counts[contract_decision] += 1
        rows.append({"scope": "production_review_queue", "local_id": local_id, "decision": contract_decision})
        operator_rows.append(
            {
                "scope": "production_review_queue",
                "local_id": local_id,
                "decision": operator_decision,
                "decided_at": "2026-05-14T03:00:00.000Z",
                "note_zh": "",
            }
        )
    return {
        "schema": "scan-qc-review-decisions.local.v1",
        "source_type": "production_workbench",
        "source_target_count": len(rows),
        "generated_in_browser": True,
        "operator_name": "复核员",
        "privacy": {"summary_only": True},
        "aggregate_counts": {
            "total_batches": 1,
            "total_findings": len(rows),
            "p0": 1,
            "p1": 1,
            "p2": 1,
            "review_completion": {
                "total": len(rows),
                "reviewed": len(rows),
                "pending": 0,
                "complete": True,
                "counts": counts,
            },
        },
        "review_counts": counts,
        "reviewed_targets": len(rows),
        "operator_decisions": operator_rows,
        "decisions": rows,
    }


def _assert_workbench_decision_contract() -> list[str]:
    html = WORKBENCH.read_text(encoding="utf-8")
    status_copies: list[str] = []
    for operator_decision, contract_decision, label_zh in OPERATOR_DECISIONS:
        _assert(
            f'<button type="button" class="' in html and f'data-decision="{operator_decision}"' in html,
            f"missing operator button for {operator_decision}",
        )
        _assert(label_zh in html, f"missing Chinese operator label for {operator_decision}")
        _assert(
            re.search(rf"{operator_decision}:\s*\"{contract_decision}\"", html),
            f"missing contract mapping for {operator_decision}",
        )
        _assert(
            re.search(rf"{operator_decision}:\s*\"{label_zh}\"", html),
            f"missing Chinese decision label mapping for {operator_decision}",
        )
        status_copies.append(f"已记录：{label_zh}")
    for token in [
        "function hasActivePendingReview()",
        "`已记录：${recordedLabel}。已自动显示下一张待确认图片。已确认 ${reviewedCount()} 张，还需确认 ${pendingCount()} 张。`",
        "`已记录：${recordedLabel}。所有待确认图片都已确认，可以点击完成并导出结果。`",
        "`已决定 ${reviewedCount()} 项，待决定 ${pending} 项`",
        "renderPreview(activeItem, Boolean(activeItem));",
        "button.disabled = !activeItem;",
        "#decisionActions button",
    ]:
        _assert(token in html, f"missing Chinese review status template: {token}")
    return status_copies


def _assert_no_private_terms(payload: Any, label: str) -> None:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    leaked = sorted(term for term in PRIVATE_TERMS if term.lower() in text.lower())
    _assert(not leaked, f"{label} includes forbidden private terms: {', '.join(leaked)}")


def run_smoke() -> dict[str, Any]:
    status_copies = _assert_workbench_decision_contract()
    with tempfile.TemporaryDirectory(prefix="production-review-decisions-smoke-") as temp_dir:
        root = Path(temp_dir)
        input_dir = root / "input"
        output_dir = root / "output"
        metadata_dir = root / "metadata"
        processed_dir = output_dir / "images"
        input_dir.mkdir()
        processed_dir.mkdir(parents=True)
        for index in range(1, 4):
            filename = f"smoke-page-{index:03d}.png"
            (input_dir / filename).write_bytes(b"synthetic original preview")
            (processed_dir / filename).write_bytes(b"synthetic processed preview")
        _write_json(metadata_dir / PRODUCTION_REVIEW_QUEUE_JSON, _fake_queue())

        controller = WorkbenchController()
        controller.configure(input_dir, output_dir, metadata_dir, processing_mode="standard")
        status = controller.status()
        queue = status.get("queue") if isinstance(status.get("queue"), dict) else {}
        items = queue.get("items") if isinstance(queue.get("items"), list) else []
        local_ids = [str(item.get("local_id")) for item in items if isinstance(item, dict)]

        _assert(queue.get("summary", {}).get("ready_for_operator_review") is True, "review queue is not ready")
        _assert(len(local_ids) == 3, "review queue item count changed")
        _assert(
            all(isinstance(item, dict) and item.get("preview_source") == "comparison" for item in items),
            "review queue preview readiness changed",
        )

        summary = _decision_summary(local_ids)
        result = controller.save_draft_review_decisions(summary)
        decision_summary = result.get("decision_summary") if isinstance(result.get("decision_summary"), dict) else {}
        saved_draft = json.loads((metadata_dir / REVIEW_DECISION_DRAFT_JSON).read_text(encoding="utf-8"))

        _assert(result.get("saved") is True, "draft decisions were not saved")
        _assert(result.get("message_zh") == "已自动保存", "draft save Chinese status copy changed")
        _assert(decision_summary.get("completion_status") == "complete", "decision summary did not complete")
        _assert(decision_summary.get("total_decisions") == 3, "decision total changed")
        _assert(decision_summary.get("pending") == 0, "pending decision count changed")
        _assert(decision_summary.get("decision_counts", {}).get("false_positive") == 1, "pass aggregate changed")
        _assert(decision_summary.get("decision_counts", {}).get("needs_rescan") == 1, "rescan aggregate changed")
        _assert(decision_summary.get("decision_counts", {}).get("fixed_externally") == 1, "reprocess aggregate changed")
        _assert(saved_draft.get("privacy", {}).get("summary_only") is True, "saved draft is not summary-only")
        _assert(saved_draft.get("reviewed_targets") == 3, "saved draft reviewed count changed")
        _assert(saved_draft.get("aggregate_counts", {}).get("review_completion", {}).get("pending") == 0, "saved draft pending count changed")
        _assert_no_private_terms(saved_draft, "saved draft decision summary")

        public_evidence = {
            "status": "pass",
            "queue_ready": True,
            "decision_labels_zh": status_copies,
            "review_items": decision_summary.get("total_decisions"),
            "reviewed_items": decision_summary.get("total_decisions", 0) - decision_summary.get("pending", 0),
            "pending_items": decision_summary.get("pending"),
            "false_positive": decision_summary.get("decision_counts", {}).get("false_positive"),
            "needs_rescan": decision_summary.get("decision_counts", {}).get("needs_rescan"),
            "fixed_externally": decision_summary.get("decision_counts", {}).get("fixed_externally"),
            "summary_only": saved_draft.get("privacy", {}).get("summary_only") is True,
            "draft_status_zh": result.get("message_zh"),
        }
        _assert_no_private_terms(public_evidence, "smoke stdout evidence")
        return public_evidence


def main() -> int:
    try:
        result = run_smoke()
    except AssertionError as exc:
        print(f"Production workbench review-decision smoke failed: {exc}", file=sys.stderr)
        return 1
    print("Production workbench review-decision smoke passed")
    print(
        "queue_ready={queue_ready} labels={labels} review_items={review_items} "
        "reviewed={reviewed_items} pending={pending_items} pass={false_positive} "
        "rescan={needs_rescan} reprocess={fixed_externally} summary_only={summary_only} "
        "draft_status={draft_status_zh}".format(labels=",".join(result["decision_labels_zh"]), **result)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
