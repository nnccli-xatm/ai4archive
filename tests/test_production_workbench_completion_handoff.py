from __future__ import annotations

import json
from pathlib import Path

from archive_scan_qc.local_workbench import (
    PRODUCTION_RUN_SUMMARY_JSON,
    WorkbenchController,
)


def _decision_summary() -> dict:
    return {
        "schema": "scan-qc-review-decisions.local.v1",
        "source_type": "production_workbench",
        "source_target_count": 1,
        "generated_in_browser": True,
        "privacy": {"summary_only": True},
        "aggregate_counts": {
            "review_completion": {
                "total": 1,
                "reviewed": 1,
                "pending": 0,
                "complete": True,
                "counts": {
                    "pending": 0,
                    "accepted_issue": 0,
                    "false_positive": 1,
                    "fixed_externally": 0,
                    "needs_rescan": 0,
                    "blocked": 0,
                },
            }
        },
        "review_counts": {
            "pending": 0,
            "accepted_issue": 0,
            "false_positive": 1,
            "fixed_externally": 0,
            "needs_rescan": 0,
            "blocked": 0,
        },
        "reviewed_targets": 1,
        "decisions": [{"scope": "production_review_queue", "local_id": "PRQ-1", "decision": "false_positive"}],
        "operator_name": "复核员",
    }


def _write_summary(metadata_dir: Path, output_dir: Path, include_handoff: bool) -> None:
    payload = {
        "operator_summary": {"total_source_images": 1, "derivative_images_ready": 1},
        "counts": {"total_files": 1, "openable_files": 1, "processed_files": 1, "resumed_files": 0},
        "artifacts": {"processing_audit_summary": str(output_dir / "processing_audit_summary.json")},
    }
    (metadata_dir / PRODUCTION_RUN_SUMMARY_JSON).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    if include_handoff:
        (output_dir / "processing_audit_summary.json").write_text(
            json.dumps(
                {
                    "conservative_auto_retouch_handoff_zh": {
                        "title_zh": "保守自动修复决策汇总",
                        "aggregate_only": True,
                        "decision_counts_zh": {"保护保留": 1},
                        "operation_reason_class_counts_zh": [],
                        "message_zh": "保守决策汇总：保护保留 1 次。",
                        "privacy_note_zh": "仅包含聚合计数。",
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )


def test_completion_export_includes_conservative_handoff_when_available(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    metadata_dir = tmp_path / "metadata"
    input_dir.mkdir()
    controller = WorkbenchController()
    controller.configure(input_dir, output_dir, metadata_dir)
    _write_summary(metadata_dir, output_dir, include_handoff=True)

    result = controller.save_review_decisions(_decision_summary())

    handoff = result["completion_panel"].get("conservative_auto_retouch_handoff_zh")
    assert isinstance(handoff, dict)
    assert handoff.get("aggregate_only") is True
    assert "source_relative_path" not in json.dumps(handoff, ensure_ascii=False)


def test_completion_export_handles_missing_conservative_handoff(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    metadata_dir = tmp_path / "metadata"
    input_dir.mkdir()
    controller = WorkbenchController()
    controller.configure(input_dir, output_dir, metadata_dir)
    _write_summary(metadata_dir, output_dir, include_handoff=False)

    result = controller.save_review_decisions(_decision_summary())

    assert result["finished"] is True
    assert "conservative_auto_retouch_handoff_zh" not in result["completion_panel"]
