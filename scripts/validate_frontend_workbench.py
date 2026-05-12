"""Validate the static scan-QC frontend workbench prototype."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
WORKBENCH = ROOT / "docs" / "frontend-workbench-prototype.html"

REQUIRED_REGIONS = {
    "artifact-loader",
    "overview-metrics",
    "workflow-steps",
    "aggregate-handoff",
    "review-decisions",
    "batch-list",
    "findings-list",
    "batch-detail",
    "image-preview",
}

REQUIRED_STRINGS = {
    "run_plan_summary.json",
    "scan_qc_report.json",
    "workbench_public_summary.json",
    "scan-qc.workbench-public-summary.v1",
    "Workbench public summary",
    "review_summary.json",
    "acceptance_summary.json",
    "aggregate_evidence_bundle_summary.json",
    "final_production_handoff_summary.json",
    "release_candidate_summary.json",
    "deep_inspection_candidate_summary.json",
    "scan-qc.deep-inspection-candidates.v1",
    "深度检查候选摘要",
    "深度检查候选摘要",
    "候选总数",
    "按原因统计候选",
    "按严重级别统计候选",
    "未运行推理",
    "public_safe_validation_index.json",
    "scan-qc.public-safe-validation-index.v1",
    "frontend_workbench_validation.json",
    "Frontend workbench validation summary",
    "前端工作台验证摘要",
    "覆盖布尔项",
    "隐私布尔项",
    "验证错误代码",
    "已验证 HTML 路径",
    "scan-qc.processing-review.v1",
    "Processing-review package summary",
    "处理复核包摘要",
    "已处理数",
    "失败数",
    "复核目标数",
    "仅本地状态",
    "敏感性/仅本地状态",
    "处理状态计数",
    "处理已恢复",
    "处理重复复用",
    "处理已有衍生复用",
    "处理操作耗时热点",
    "耗时秒数",
    "文件/分钟",
    "平均秒/文件",
    "aggregateProcessingOperationTimings",
    "processingOperationTimingPanel",
    "despeckle_backend",
    "aggregateDespeckleBackend",
    "去噪点后端摘要",
    "请求后端",
    "有效后端模式",
    "回退数",
    "请求 NumPy 后回退数",
    "去噪点后端警告代码",
    "despeckle_numpy_unavailable_fallback",
    "despeckle_numpy_requested_all_fallback",
    "synthetic review IDs only",
    "row-level private notes",
    "processing_review",
    "Public-safe validation index",
    "公开安全验证索引",
    "工件存在数",
    "工件失败数",
    "工件缺失数",
    "未知输入",
    "验证检查通过数",
    "验证检查失败数",
    "验证阻塞项数",
    "隐私仅汇总状态",
    "JSON.parse",
    "type=\"file\"",
    "No artifact loaded",
    "无法加载 JSON",
    "原始预览占位",
    "处理后预览占位",
    "本地图像预览脚手架",
    "原始/来源图像",
    "处理后/质检输出图像",
    "预览状态：",
    "清除原始预览",
    "清除处理后预览",
    "适应面板",
    "重置缩放",
    "预览缩放级别",
    "预览显示：适应面板",
    "预览本地状态不包含在复核决定导出 JSON 中。",
    "URL.createObjectURL",
    "URL.revokeObjectURL",
    "beforeunload",
    "preview filename",
    "preview object URL",
    "preview display mode",
    "preview zoom level",
    "人工复核决定",
    "reviewTargetList",
    "目标总数",
    "已复核目标",
    "待处理目标",
    "完成状态",
    "未完成",
    "复核目标筛选",
    "reviewDecisionFilter",
    "reviewScopeFilter",
    "reviewSeverityFilter",
    "reviewStatusFilter",
    "reviewFilterCount",
    "显示 0 / 0 个目标",
    "显示 ${visibleTargets.length} / ${targets.length} 个目标",
    "没有复核目标匹配当前筛选条件。",
    "筛选后的批量复核决定操作",
    "批量设置可见决定",
    "应用到可见目标",
    "尚未应用筛选后的批量决定。",
    "批量操作只更新此浏览器标签页中的可见目标。",
    "可见目标：",
    "已更新目标：",
    "applyBulkVisibleReviewDecision",
    "Severity",
    "Status",
    "决定状态",
    "导入隐私安全摘要",
    "reviewImportFile",
    "reviewImportStatus",
    "尚未导入复核决定摘要。",
    "导入只恢复范围、合成/本地 ID 和决定状态",
    "已导入",
    "skipped",
    "最近导入",
    "架构不匹配：预期 scan-qc-review-decisions.local.v1。",
    "来源不匹配：摘要 source_type 与已加载工件不一致。",
    "目标数量不匹配",
    "导入决定前请先加载扫描、运行计划或汇总交接工件。",
    "验证代码",
    "duplicate_decision",
    "ignored_private_field",
    "ignored_extra_field",
    "unsupported_decision_status",
    "unknown_review_target",
    "invalid_decision_entry",
    "accepted_issue",
    "false_positive",
    "fixed_externally",
    "needs_rescan",
    "scan-qc-review-decisions.local.v1",
    "生成隐私安全摘要",
    "reviewCompletionGate",
    "复核完成门禁警告",
    "完成门禁警告仅使用汇总计数",
    "导出仅供参考且仍可使用。",
    "导出保持隐私安全且架构稳定。",
    "决定状态计数",
    "renderReviewCompletionGate",
    "reviewDecisionCountText",
    "generated_in_browser",
    "source_target_count",
    "parseReviewDecisionSummary",
    "applyReviewDecisionSummary",
    "importReviewDecisionFile",
    "scope",
    "local_id",
    "decision",
    "汇总工件摘要",
    "仅在本地策略复核后查看公开安全的汇总摘要",
    "Review summary",
    "Acceptance summary",
    "review-summary",
    "acceptance-summary",
    "仅汇总状态",
    "工作流状态",
    "工件类型",
    "剩余 P0",
    "剩余 P1",
    "复核状态计数",
    "严重级别状态计数",
    "未提供严重级别/状态计数。",
    "规则计数",
    "规则状态计数",
    "验收通过",
    "阻塞与警告代码",
    "阻塞代码",
    "警告代码",
    "阻塞代码",
    "警告代码",
    "未提供按汇总代码统计的阻塞计数。",
    "未提供按汇总代码统计的警告计数。",
    "警告数",
    "扫描工作线程",
    "处理工作线程",
    "去噪点后端摘要",
    "后端模式",
    "NumPy 可用",
    "后端计数",
    "提供方能力探测",
    "提供方数",
    "已配置提供方数",
    "已禁用提供方数",
    "提供方已配置",
    "可见 GPU 数",
    "可见模型数",
    "可见可选包数",
    "缺失可选包数",
    "探测隐私状态",
    "aggregateWarningCodes",
    "aggregateCodeCounts",
    "aggregateNestedStatusCounts",
    "aggregateWorkers",
    "aggregateProviderProbe",
    "aggregateProcessingReview",
    "buildProcessingReviewTargets",
    "aggregatePrivacyOmissions",
    "privacyOmits",
    "隐私自检状态",
    "隐私自检违规数",
    "Aggregate-only status from locally reviewed public-safe summary artifacts.",
    "公开安全工件兼容性诊断",
    "Compatibility diagnostic uses aggregate/public-safe fields only",
    "已识别工件类型",
    "架构版本",
    "架构/类型检测",
    "生成时间戳存在性",
    "隐私摘要",
    "诊断阻塞数",
    "诊断警告数",
    "存在的预期汇总状态字段",
    "缺失的预期汇总状态字段",
    "unsupported_public_safe_schema_version",
    "generated_timestamp_missing",
    "aggregate_status_fields_missing",
    "privacy_summary_missing",
    "privacy_summary_fail",
    "artifact_compatibility_pass",
    "SUPPORTED_PUBLIC_SAFE_SCHEMA_PREFIXES",
    "buildArtifactCompatibilityDiagnostics",
    "公开安全工件就绪清单",
    "顶层就绪状态",
    "公开交接就绪",
    "公开交接未就绪",
    "预期工件",
    "缺失工件",
    "隐私阻塞项",
    "过期工件",
    "存在/缺失",
    "生成时间戳",
    "artifact_readiness_checklist",
    "public_safe_artifact_readiness",
    "EXPECTED_PUBLIC_SAFE_ARTIFACTS",
    "buildArtifactReadinessChecklist",
    "artifactReadinessPanel",
    "excludes local preview filename, preview content, and object URL state",
    "工件存在与状态",
    "隐私状态",
    "敏感性",
    "已省略私有证据",
    "交接就绪",
    "发布候选就绪",
    "阻塞项",
    "检查通过数",
    "检查失败数",
    "扫描吞吐",
    "处理吞吐",
    "contains_paths",
    "contains_filenames",
    "contains_hashes",
    "contains_ocr_text",
    "contains_thumbnails",
    "contains_image_content",
    "contains_row_level_findings",
    "private filenames, paths, hashes, OCR text, thumbnails, image content, row-level findings, reviewer notes, manifests, or derivative image references",
    "公开安全演示夹具库",
    "用于浏览器验证的合成纯汇总夹具。",
    "演示夹具不包含私有文件名、路径、哈希、OCR 文本、缩略图、图像内容、清单、行级发现、复核备注、衍生图像引用和本地预览状态。",
    "demoFixtureSelect",
    "loadDemoFixtureButton",
    "DEMO_FIXTURES",
    "loadDemoFixture",
    "cloneDemoPayload",
    "已识别的通过复核摘要",
    "通过的验收摘要",
    "完整的公开安全就绪清单",
    "合成处理复核包摘要",
    "不支持架构的兼容性警告",
    "隐私摘要失败诊断",
    "隐私摘要缺失诊断",
    "已加载公开安全演示夹具",
    "unsupported_input",
}

FORBIDDEN_PATTERNS = {
    "absolute unix user path": re.compile(r"/Users/[A-Za-z0-9._-]+/"),
    "windows drive path": re.compile(r"\b[A-Za-z]:\\\\"),
    "sha256-like hash": re.compile(r"\b[a-fA-F0-9]{64}\b"),
    "github token": re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    "openai key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "aws access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "generic bearer token": re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{20,}\b", re.IGNORECASE),
    "embedded image data": re.compile(r"\bdata:image/", re.IGNORECASE),
    "remote image url": re.compile(r"https?://[^\s\"']+\.(?:jpg|jpeg|png|tif|tiff|webp)\b", re.IGNORECASE),
    "local file image url": re.compile(r"\bfile://[^\s\"']+", re.IGNORECASE),
    "base64 image field": re.compile(r"\bimage_(?:data|base64|bytes)\b", re.IGNORECASE),
    "ocr text field": re.compile(r"\bocr_text\b", re.IGNORECASE),
    "private image filename": re.compile(r"\b[\w.-]+\.(?:jpg|jpeg|png|tif|tiff|webp)\b", re.IGNORECASE),
}

FORBIDDEN_EXPORT_FIELDS = {
    "ocr_text",
    "hash",
    "sha256",
    "thumbnail",
    "absolute_path",
    "image_bytes",
    "manifest",
    "reviewer_notes",
    "derivative_image",
}

FORBIDDEN_PREVIEW_FIELDS = {
    "preview_filename",
    "preview_object_url",
    "preview_display_mode",
    "preview_zoom_level",
}

REQUIRED_AGGREGATE_FIELDS = {
    "acceptance pass/fail": "acceptancePassed",
    "artifact type": "aggregateArtifactType",
    "blocking codes": "blockingCodes",
    "blocking item count": "blockingItemCount",
    "privacy/sensitivity": "aggregatePrivacy",
    "remaining p0": "remainingP0",
    "remaining p1": "remainingP1",
    "review status counts": "reviewStatusCounts",
    "rule counts": "ruleCounts",
    "rule status counts": "ruleStatusCounts",
    "severity status counts": "severityStatusCounts",
    "throughput": "aggregateThroughput",
    "warning codes": "warningCodes",
    "warning count": "warningCount",
    "workers": "aggregateWorkers",
    "compatibility diagnostics": "buildArtifactCompatibilityDiagnostics",
    "deep inspection candidate summary": "aggregateDeepInspectionCandidateSummary",
    "deep inspection candidate source": "deepInspectionCandidateSource",
    "frontend workbench validation summary": "aggregateFrontendValidationSummary",
    "provider capability probe": "aggregateProviderProbe",
    "processing review summary": "aggregateProcessingReview",
    "processing review targets": "buildProcessingReviewTargets",
    "review decision verification summary": "aggregateReviewDecisionVerificationSummary",
    "despeckle backend capability": "aggregateDespeckleBackend",
}

REQUIRED_CHECKLIST_FIELDS = {
    "expected artifacts": "EXPECTED_PUBLIC_SAFE_ARTIFACTS",
    "synthetic checklist input": "artifact_readiness_checklist",
    "alternate checklist input": "public_safe_artifact_readiness",
    "readiness model": "buildArtifactReadinessChecklist",
    "ready summary": "公开交接就绪",
    "not-ready summary": "公开交接未就绪",
    "privacy status": "privacyStatus",
    "generated timestamp": "generatedAt",
    "blocking count": "blockingCount",
    "warning count": "warningCount",
    "stale count": "staleCount",
}

REQUIRED_COMPATIBILITY_FIELDS = {
    "recognized artifact type": "已识别工件类型",
    "schema version": "架构版本",
    "schema/type detection": "架构/类型检测",
    "generated timestamp presence": "生成时间戳存在性",
    "privacy summary": "隐私摘要",
    "blocking diagnostic count": "诊断阻塞数",
    "warning diagnostic count": "诊断警告数",
    "expected fields present": "存在的预期汇总状态字段",
    "expected fields missing": "缺失的预期汇总状态字段",
    "unsupported schema warning": "unsupported_public_safe_schema_version",
    "privacy missing diagnostic": "privacy_summary_missing",
    "privacy failing diagnostic": "privacy_summary_fail",
}

FORBIDDEN_AGGREGATE_PAYLOAD_FIELDS = {
    "content hash": "hash",
    "derivative image": "derivative_image",
    "image content": "image_content",
    "manifest rows": "manifest",
    "ocr text": "ocr_text",
    "private filename": "filename",
    "private path": "path",
    "reviewer notes": "reviewer_notes",
    "row-level findings": "findings",
    "thumbnail": "thumbnail",
}

REQUIRED_DEMO_FIXTURE_LABELS = {
    "已识别的通过复核摘要",
    "通过的工作台公开摘要",
    "阻塞的工作台公开摘要",
    "通过的复核决定验证摘要",
    "阻塞的复核决定验证摘要",
    "通过的验收摘要",
    "完整的公开安全就绪清单",
    "通过的最终生产交接",
    "阻塞的最终生产交接",
    "深度检查候选摘要",
    "已禁用的提供方能力探测",
    "通过的公开安全验证索引",
    "阻塞的公开安全验证索引",
    "合成处理复核包摘要",
    "不支持架构的兼容性警告",
    "隐私摘要失败诊断",
    "隐私摘要缺失诊断",
}

REQUIRED_PREVIEW_LIFECYCLE_STRINGS = {
    "clear function": "function clearPreviewState",
    "clear all function": "function clearAllPreviewState",
    "load function": "function loadPreviewFile",
    "fit function": "function setPreviewFitMode",
    "zoom function": "function setPreviewZoom",
    "reset zoom function": "function resetPreviewZoom",
    "create object URL": "URL.createObjectURL(file)",
    "clear revocation": "URL.revokeObjectURL(previewState.objectUrl)",
    "replacement revocation": "if (previewState.objectUrl)",
    "beforeunload revocation": 'window.addEventListener("beforeunload"',
    "export exclusion": "预览本地状态不包含在复核决定导出 JSON 中。",
    "local tab copy": "浏览器标签页",
    "original slot": "originalPreviewFile",
    "processed slot": "processedPreviewFile",
    "fit control": "fitPreviewButton",
    "zoom control": "previewZoomSelect",
    "zoom status": "previewZoomStatus",
    "zoom class": "preview-zoom",
    "preview slot child width cap": ".preview-slot > *",
    "preview file input width cap": '.preview-slot input[type="file"]',
    "preview max width cap": "max-width: 100%",
    "preview min width reset": "min-width: 0",
}

FORBIDDEN_DEMO_FIXTURE_FIELDS = {
    "absolute_path",
    "derivative_image",
    "filename",
    "hash",
    "image_bytes",
    "image_content",
    "manifest",
    "ocr_text",
    "path",
    "preview_filename",
    "preview_object_url",
    "preview_display_mode",
    "preview_zoom_level",
    "reviewer_notes",
    "sha256",
    "thumbnail",
}

PRIVATE_OUTPUT_PATTERNS = (
    re.compile(r"blob:[^\s\"'<>]+", re.IGNORECASE),
    re.compile(r"/Users/[A-Za-z0-9._/-]+"),
    re.compile(r"/private/[A-Za-z0-9._/-]+"),
    re.compile(r"\b[A-Za-z]:\\\\[^\s\"'<>]+"),
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print a deterministic public-safe JSON validation summary to stdout.",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        help="Write a deterministic public-safe JSON validation summary to this path.",
    )
    parser.add_argument(
        "--self-test-json",
        action="store_true",
        help="Run focused self-tests for JSON success and synthetic failure output.",
    )
    parser.add_argument(
        "--workbench",
        type=Path,
        default=WORKBENCH,
        help=argparse.SUPPRESS,
    )
    return parser


def new_summary(workbench: Path) -> dict[str, Any]:
    return {
        "status": "fail",
        "validated_html_path": safe_workbench_path(workbench),
        "counts": {
            "required_regions": len(REQUIRED_REGIONS),
            "required_strings": len(REQUIRED_STRINGS),
            "required_aggregate_fields": len(REQUIRED_AGGREGATE_FIELDS),
            "required_checklist_fields": len(REQUIRED_CHECKLIST_FIELDS),
            "required_compatibility_fields": len(REQUIRED_COMPATIBILITY_FIELDS),
            "required_demo_fixture_labels": len(REQUIRED_DEMO_FIXTURE_LABELS),
            "required_preview_lifecycle_strings": len(REQUIRED_PREVIEW_LIFECYCLE_STRINGS),
            "forbidden_pattern_checks": len(FORBIDDEN_PATTERNS),
            "forbidden_export_field_checks": len(FORBIDDEN_EXPORT_FIELDS),
            "forbidden_preview_field_checks": len(FORBIDDEN_PREVIEW_FIELDS),
            "forbidden_aggregate_payload_field_checks": len(FORBIDDEN_AGGREGATE_PAYLOAD_FIELDS),
            "forbidden_demo_fixture_field_checks": len(FORBIDDEN_DEMO_FIXTURE_FIELDS),
            "executable_preview_lifecycle_checks": 6,
        },
        "fixture_groups": {
            "aggregate_executable_fixture_groups": 12,
            "demo_fixture_labels_required": len(REQUIRED_DEMO_FIXTURE_LABELS),
            "preview_lifecycle_synthetic_slots": 2,
        },
        "coverage": {
            "aggregate_summary": False,
            "review_acceptance": False,
            "review_decision_import_export": False,
            "compatibility_diagnostics": False,
            "readiness_checklist": False,
            "demo_fixtures": False,
            "final_handoff_fixtures": False,
            "provider_capability_probe": False,
            "executable_fixtures": False,
            "preview_lifecycle": False,
        },
        "privacy": {
            "forbidden_pattern_checks_passed": False,
            "review_export_forbidden_field_checks_passed": False,
            "review_import_forbidden_field_checks_passed": False,
            "aggregate_payload_forbidden_field_checks_passed": False,
            "demo_fixture_forbidden_field_checks_passed": False,
            "preview_lifecycle_public_safe": False,
            "forbidden_field_check_count": (
                len(FORBIDDEN_EXPORT_FIELDS) * 2
                + len(FORBIDDEN_PREVIEW_FIELDS) * 3
                + len(FORBIDDEN_AGGREGATE_PAYLOAD_FIELDS)
                + len(FORBIDDEN_DEMO_FIXTURE_FIELDS)
            ),
        },
        "error_count": 0,
        "errors": [],
    }


def safe_workbench_path(workbench: Path) -> str:
    try:
        return str(workbench.resolve().relative_to(ROOT))
    except ValueError:
        return workbench.name


def add_error(summary: dict[str, Any], code: str, message: str) -> None:
    summary["errors"].append({"code": code, "message": sanitize_public_message(message)})


def sanitize_public_message(message: str) -> str:
    safe = message
    for pattern in PRIVATE_OUTPUT_PATTERNS:
        safe = pattern.sub("[redacted-private-value]", safe)
    return safe


def validate_executable_aggregate_fixtures(html: str) -> list[str]:
    script_match = re.search(r"<script>(?P<script>.*?)</script>", html, re.DOTALL)
    if not script_match:
        return ["missing executable workbench script"]

    if str(ROOT / "src") not in sys.path:
        sys.path.insert(0, str(ROOT / "src"))
    from archive_scan_qc.workbench_summary import build_workbench_public_summary

    with tempfile.TemporaryDirectory() as tmp_dir:
        unsupported_private_input = Path(tmp_dir) / "private_scan_alpha.tif"
        unsupported_private_input.write_text("PRIVATE_OCR_TEXT", encoding="utf-8")
        actual_workbench_summary = build_workbench_public_summary(
            files=[unsupported_private_input],
            generated_at="2026-05-11T02:20:00Z",
        )
    actual_workbench_summary_json = json.dumps(actual_workbench_summary, sort_keys=True)

    with tempfile.TemporaryDirectory() as tmp_dir:
        aggregate_dir = Path(tmp_dir)
        aggregate_payloads = {
            "workbench_public_summary.json": {
                "schema_version": "scan-qc.workbench-public-summary.v1",
                "generated_at": "2026-05-11T02:30:00Z",
                "status": "pass",
                "ready": True,
                "privacy": {"aggregate_only": True, "redacts_private_values": True},
            },
            "aggregate_evidence_bundle_summary.json": {
                "schema_version": "scan-qc.aggregate-evidence-bundle.v1",
                "generated_at": "2026-05-11T02:31:00Z",
                "status": "pass",
                "ready": True,
                "privacy": {"aggregate_only": True, "redacts_private_values": True},
            },
            "final_production_handoff_summary.json": {
                "schema_version": "scan-qc.final-production-handoff-summary.v1",
                "generated_at": "2026-05-11T02:32:00Z",
                "status": "pass",
                "ready_for_handoff": True,
                "privacy": {"aggregate_only": True, "redacts_private_values": True},
            },
            "public_safe_validation_index.json": {
                "schema_version": "scan-qc.public-safe-validation-index.v1",
                "generated_at": "2026-05-11T02:33:00Z",
                "status": "pass",
                "ready": True,
                "privacy": {"aggregate_only": True, "redacts_private_values": True},
            },
        }
        for name, payload in aggregate_payloads.items():
            (aggregate_dir / name).write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT / "src")
        command_result = subprocess.run(
            [
                sys.executable,
                "-m",
                "archive_scan_qc",
                "artifact-readiness-checklist",
                "--evidence-dir",
                str(aggregate_dir),
                "--out",
                str(aggregate_dir),
            ],
            cwd=ROOT,
            env=env,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if command_result.returncode != 0:
            return [f"artifact-readiness-checklist command failed with code {command_result.returncode}"]
        generated_checklist = json.loads((aggregate_dir / "artifact_readiness_checklist.json").read_text(encoding="utf-8"))
        checklist_rows = generated_checklist.get("artifact_readiness_checklist", {})
        if generated_checklist.get("summary", {}).get("required_missing_count") != 0:
            return ["artifact-readiness-checklist command did not preserve aggregate required missing count"]
        if generated_checklist.get("blocking_counts_by_code") != {}:
            return ["artifact-readiness-checklist command did not preserve aggregate blocking code counts"]
        if checklist_rows.get("workbench_public_summary.json", {}).get("category") != "workbench_public_summary":
            return ["artifact-readiness-checklist command did not generate workbench public summary readiness row"]
        if not generated_checklist.get("privacy", {}).get("aggregate_only"):
            return ["artifact-readiness-checklist command did not mark aggregate-only privacy"]

    runner = f"""
const vm = require("node:vm");
const workbenchScript = {script_match.group("script")!r};
const elements = new Map();

function element(id) {{
  if (!elements.has(id)) {{
    elements.set(id, {{
      id,
      value: "",
      innerHTML: "",
      textContent: "",
      className: "",
      files: [],
      classList: {{
        add() {{}},
        remove() {{}}
      }},
      addEventListener() {{}},
      querySelectorAll() {{
        return [];
      }},
      click() {{}}
    }});
  }}
  return elements.get(id);
}}

const context = {{
  actualGeneratedWorkbenchFixture: {actual_workbench_summary_json},
  assert,
  assertPublicSafe,
  countFor,
  console,
  Blob: function Blob() {{}},
  URL: {{
    createObjectURL() {{ return "blob:aggregate-fixture"; }},
    revokeObjectURL() {{}}
  }},
  document: {{
    getElementById: element,
    createElement: element
  }},
  window: {{
    addEventListener() {{}}
  }}
}};

function assert(condition, message) {{
  if (!condition) throw new Error(message);
}}

function assertPublicSafe(value, label) {{
  const text = typeof value === "string" ? value : JSON.stringify(value);
  [
    "/Users/",
    "C:\\\\",
    "PRIVATE_OCR_TEXT",
    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "private_scan_alpha.tif",
    "blob:synthetic-preview"
  ].forEach(token => assert(!text.includes(token), label + " leaked " + token));
}}

function countFor(rows, name) {{
  const row = rows.find(item => item.name === name);
  return row ? row.count : undefined;
}}

vm.createContext(context);
vm.runInContext(workbenchScript + `
  const reviewFixture = {{
    schema_version: "scan-qc-review-summary.v1",
    generated_at: "2026-05-11T00:00:00Z",
    status: "pass",
    blocking_item_count: 0,
    blocking_items: [],
    warnings: [],
    remaining_p0: 0,
    remaining_p1: 1,
    total_findings: 8,
    status_counts: {{
      accepted_issue: 2,
      false_positive: 5,
      needs_rescan: 1
    }},
    severity_status_counts: {{
      P0: {{ accepted_issue: 0, false_positive: 0 }},
      P1: {{ accepted_issue: 1, false_positive: 1 }},
      P2: {{ accepted_issue: 1, false_positive: 4, needs_rescan: 1 }}
    }},
    rule_counts: {{
      skew_detected: 3,
      blur_detected: 5
    }},
    rule_status_counts: {{
      skew_detected: {{ false_positive: 3 }},
      blur_detected: {{ accepted_issue: 2, false_positive: 2, needs_rescan: 1 }}
    }},
    privacy: {{
      aggregate_only: true,
      omits: [
        "source location strings",
        "source file identifiers",
        "content hashes",
        "recognized text",
        "thumbnails",
        "image content",
        "row-level findings"
      ],
      contains_paths: false,
      contains_filenames: false,
      contains_hashes: false,
      contains_ocr_text: false,
      contains_thumbnails: false,
      contains_image_content: false,
      contains_row_level_findings: false,
      redacts_private_values: true
    }},
    sensitivity: "aggregate-only public summary"
  }};

  const acceptanceFixture = {{
    schema_version: "scan-qc-acceptance-summary.v1",
    generated_at: "2026-05-11T00:00:00Z",
    acceptance_passed: true,
    pass: true,
    blocking_item_count: 0,
    blocking_items: [],
    warnings: [
      {{ code: "aggregate_warning_review_backlog" }},
      {{ code: "aggregate_warning_policy_hold" }}
    ],
    human_review: {{
      remaining_p0: 0,
      remaining_p1: 2,
      total_findings: 9,
      status_counts: {{
        accepted_issue: 4,
        false_positive: 5
      }}
    }},
    privacy: {{
      aggregate_only: true,
      omits: [
        "source location strings",
        "source file identifiers",
        "content hashes",
        "recognized text",
        "thumbnails",
        "image content",
        "row-level findings"
      ],
      contains_paths: false,
      contains_filenames: false,
      contains_hashes: false,
      contains_ocr_text: false,
      contains_thumbnails: false,
      contains_image_content: false,
      contains_row_level_findings: false,
      redacts_private_values: true
    }},
    privacy_self_check: {{
      status: "passed",
      violation_count: 0
    }},
    sensitivity: "aggregate-only public summary"
  }};

  function publicSafeReadinessRow(name, category, label, generatedAt, required = false) {{
    return {{
      artifact: name,
      name,
      type: category,
      category,
      label,
      present: true,
      required,
      status: "pass",
      reported_status: "pass",
      ready: true,
      pass: true,
      generated_at: generatedAt,
      blocking_count: 0,
      warning_count: 0,
      blocking_counts_by_code: {{}},
      warning_counts_by_code: {{}},
      privacy_status: "pass",
      privacy_failure_codes: []
    }};
  }}

  const completeChecklistFixture = {{
    schema_version: "scan-qc-artifact-readiness-checklist.v1",
    generated_at: "2026-05-11T00:00:00Z",
    status: "pass",
    ready: true,
    summary: {{
      known_artifacts: 11,
      artifacts_present: 11,
      artifacts_passed: 11,
      artifacts_failed: 0,
      required_missing_count: 0,
      optional_missing_count: 0,
      missing_count: 0,
      unsupported_inputs: 0,
      blocking_count: 0,
      warning_count: 0,
      privacy_blocker_count: 0
    }},
    aggregate_counts: {{
      known_artifacts: 11,
      artifacts_present: 11,
      artifacts_passed: 11,
      artifacts_failed: 0,
      required_missing_count: 0,
      optional_missing_count: 0,
      missing_count: 0,
      unsupported_inputs: 0,
      blocking_count: 0,
      warning_count: 0,
      privacy_blocker_count: 0
    }},
    blocking_counts_by_code: {{}},
    warning_counts_by_code: {{}},
    blocking_items: [],
    warning_items: [],
    privacy: {{
      aggregate_only: true,
      redacts_private_values: true,
      private_indicators_found: false,
      private_indicator_count: 0
    }},
    sensitivity: "aggregate-only public summary",
    artifact_readiness_checklist: {{
      "run_plan_summary.json": publicSafeReadinessRow("run_plan_summary.json", "run_plan", "Run plan summary", "2026-05-11T00:00:00Z"),
      "workbench_public_summary.json": publicSafeReadinessRow("workbench_public_summary.json", "workbench_public_summary", "Workbench public summary", "2026-05-11T00:00:30Z", true),
      "review_summary.json": publicSafeReadinessRow("review_summary.json", "review_summary", "Review summary", "2026-05-11T00:01:00Z"),
      "acceptance_summary.json": publicSafeReadinessRow("acceptance_summary.json", "acceptance_summary", "Acceptance summary", "2026-05-11T00:02:00Z"),
      "aggregate_evidence_bundle_summary.json": publicSafeReadinessRow("aggregate_evidence_bundle_summary.json", "aggregate_evidence_bundle", "Aggregate evidence bundle summary", "2026-05-11T00:03:00Z", true),
      "release_candidate_summary.json": publicSafeReadinessRow("release_candidate_summary.json", "release_candidate_summary", "Release candidate summary", "2026-05-11T00:04:00Z"),
      "final_production_handoff_summary.json": publicSafeReadinessRow("final_production_handoff_summary.json", "final_production_handoff", "Final production handoff summary", "2026-05-11T00:05:00Z", true),
      "deep_inspection_candidate_summary.json": publicSafeReadinessRow("deep_inspection_candidate_summary.json", "deep_inspection_candidate_summary", "深度检查候选摘要", "2026-05-11T00:05:30Z"),
      "frontend_workbench_validation.json": publicSafeReadinessRow("frontend_workbench_validation.json", "frontend_workbench_validation", "Frontend workbench validation summary", "2026-05-11T00:05:40Z"),
      "review_decision_verification_summary.json": publicSafeReadinessRow("review_decision_verification_summary.json", "review_decision_verification", "Review decision verification summary", "2026-05-11T00:05:45Z"),
      "public_safe_validation_index.json": publicSafeReadinessRow("public_safe_validation_index.json", "public_safe_validation_index", "Public-safe validation index", "2026-05-11T00:06:00Z", true)
    }},
    public_safe_artifact_readiness: null
  }};
  completeChecklistFixture.public_safe_artifact_readiness = completeChecklistFixture.artifact_readiness_checklist;

  const finalHandoffPassFixture = {{
    schema_version: "scan-qc-final-production-handoff-summary.v1",
    generated_at: "2026-05-11T01:00:00Z",
    status: "pass",
    ready_for_handoff: true,
    checks_passed: 6,
    checks_failed: 0,
    blocking_item_count: 0,
    processing_resumed_files: 2,
    processing_duplicate_reused_files: 3,
    processing_existing_derivative_reused_files: 4,
    blocking_items: [],
    warnings: [],
    artifact_status_summary: {{
      "run_plan_summary.json": {{ present: true, required: true, status: "pass", reported_status: "current", privacy_status: "public-safe" }},
      "review_summary.json": {{ present: true, required: true, status: "pass", reported_status: "current", privacy_status: "public-safe" }},
      "acceptance_summary.json": {{ present: true, required: true, status: "pass", reported_status: "current", privacy_status: "public-safe" }},
      "aggregate_evidence_bundle_summary.json": {{ present: true, required: true, status: "pass", reported_status: "current", privacy_status: "public-safe" }},
      "release_candidate_summary.json": {{ present: true, required: true, status: "pass", reported_status: "current", privacy_status: "public-safe" }},
      "deep_inspection_candidate_summary.json": {{ present: true, required: false, status: "pass", reported_status: "pass", candidate_total: 3, candidates_by_reason: {{ processing_review_status_failed: 1, quality_scanline_candidate: 2 }}, candidates_by_severity: {{ P1: 1, P2: 2 }}, provider_configured: false, provider_count: 0, checks_passed: 5, checks_failed: 0, privacy_status: "aggregate_public_safe", no_inference_run: true, source_root: "/Users/private/archive", provider_command: "python /private/model.py", environment: {{ SECRET_TOKEN: "PRIVATE_OCR_TEXT" }} }},
      "review_decision_verification_summary.json": {{ present: true, required: false, status: "pass", reported_status: "pass", checks_passed: 3, checks_failed: 0, blocking_count: 0, warning_count: 1, decision_summary: {{ total_decisions: 19, accepted: 11, rejected: 5, rework: 2, pending: 1 }}, blocking_counts_by_code: {{}}, warning_counts_by_code: {{ ignored_extra_decision_field: 1 }}, privacy_status: "pass", local_id: "PRIVATE-LOCAL-ID", source_type: "review_decision_export", source_schema: "scan-qc-review-decisions.local.v1" }},
      "final_production_handoff_summary.json": {{ present: true, required: true, status: "pass", reported_status: "current", privacy_status: "public-safe" }}
    }},
    privacy: {{
      aggregate_only: true,
      omits: [
        "source location strings",
        "source file identifiers",
        "content hashes",
        "recognized text",
        "thumbnails",
        "image content",
        "row-level findings"
      ],
      contains_paths: false,
      contains_filenames: false,
      contains_hashes: false,
      contains_ocr_text: false,
      contains_thumbnails: false,
      contains_image_content: false,
      contains_row_level_findings: false,
      redacts_private_values: true,
      private_indicators_found: false,
      private_indicator_count: 0
    }},
    privacy_self_check: {{
      status: "passed",
      violation_count: 0
    }},
    sensitivity: "aggregate-only public summary"
  }};

  const deepInspectionCandidateFixture = {{
    schema_version: "scan-qc.deep-inspection-candidates.v1",
    generated_at: "2026-05-11T01:18:00Z",
    status: "pass",
    candidate_total: 3,
    candidates_by_reason: {{
      processing_review_status_failed: 1,
      quality_scanline_candidate: 2
    }},
    candidates_by_severity: {{
      P1: 1,
      P2: 2
    }},
    provider_configured: false,
    provider_count: 0,
    checks_passed: ["candidate_total_non_negative", "privacy_aggregate_only", "no_inference_run"],
    checks_failed: [],
    privacy_status: "aggregate_public_safe",
    no_inference_run: true,
    privacy: {{
      aggregate_only: true,
      redacts_private_values: true,
      private_indicators_found: false,
      private_indicator_count: 0
    }},
    privacy_self_check: {{
      status: "passed",
      violation_count: 0
    }},
    sensitivity: "aggregate-only public summary",
    file_name: "private_scan_alpha.tif",
    source_path: "/Users/private/archive/private_scan_alpha.tif",
    content_hash: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    ocr_text: "PRIVATE_OCR_TEXT",
    thumbnail: "blob:synthetic-preview",
    row_findings: [{{ path: "/private/source/page.png" }}],
    provider_command: "python /private/model.py",
    environment: {{ SECRET_TOKEN: "PRIVATE_OCR_TEXT" }}
  }};

  const aggregateEvidenceBundleCandidateFixture = {{
    schema_version: "scan-qc-aggregate-evidence-bundle-summary.v1",
    generated_at: "2026-05-11T01:19:00Z",
    status: "pass",
    checks_passed: 9,
    checks_failed: 0,
    blocking_item_count: 0,
    blocking_items: [],
    artifacts: {{
      "deep_inspection_candidate_summary.json": {{
        present: true,
        required: false,
        status: "pass",
        reported_status: "pass",
        candidate_total: 4,
        candidates_by_reason: {{
          processing_review_status_warning: 1,
          quality_content_edge_cutoff_candidate: 3
        }},
        candidates_by_severity: {{
          P1: 1,
          P2: 3
        }},
        provider_configured: true,
        provider_count: 2,
        checks_passed_count: 6,
        checks_failed_count: 0,
        privacy_status: "aggregate_public_safe",
        no_inference_run: true,
        manifest: [{{ path: "/Users/private/archive/private_scan_alpha.tif" }}],
        provider_command: "python /private/model.py",
        source_root: "/Users/private/archive"
      }}
    }},
    privacy: {{
      aggregate_only: true,
      redacts_private_values: true,
      private_indicators_found: false,
      private_indicator_count: 0
    }},
    privacy_self_check: {{
      status: "passed",
      violation_count: 0
    }},
    sensitivity: "aggregate-only public summary"
  }};

  const finalHandoffBlockedFixture = {{
    schema_version: "scan-qc-final-production-handoff-summary.v1",
    generated_at: "2026-05-11T01:10:00Z",
    status: "fail",
    ready_for_handoff: false,
    checks_passed: 4,
    checks_failed: 2,
    blocking_item_count: 2,
    blocking_items: [
      {{ code: "aggregate_handoff_acceptance_blocker" }},
      {{ code: "aggregate_handoff_artifact_blocker" }}
    ],
    warnings: [{{ code: "aggregate_warning_handoff_recheck" }}],
    artifact_status_summary: {{
      "run_plan_summary.json": {{ present: true, required: true, status: "pass", reported_status: "current", privacy_status: "public-safe" }},
      "review_summary.json": {{ present: true, required: true, status: "pass", reported_status: "current", privacy_status: "public-safe" }},
      "acceptance_summary.json": {{ present: true, required: true, status: "fail", reported_status: "blocked", privacy_status: "public-safe" }},
      "aggregate_evidence_bundle_summary.json": {{ present: true, required: true, status: "pass", reported_status: "current", privacy_status: "public-safe" }},
      "release_candidate_summary.json": {{ present: true, required: true, status: "blocked", reported_status: "blocked", privacy_status: "public-safe" }},
      "final_production_handoff_summary.json": {{ present: true, required: true, status: "fail", reported_status: "blocked", privacy_status: "public-safe" }}
    }},
    privacy: {{
      aggregate_only: true,
      omits: [
        "source location strings",
        "source file identifiers",
        "content hashes",
        "recognized text",
        "thumbnails",
        "image content",
        "row-level findings"
      ],
      contains_paths: false,
      contains_filenames: false,
      contains_hashes: false,
      contains_ocr_text: false,
      contains_thumbnails: false,
      contains_image_content: false,
      contains_row_level_findings: false,
      redacts_private_values: true,
      private_indicators_found: false,
      private_indicator_count: 0
    }},
    privacy_self_check: {{
      status: "passed",
      violation_count: 0
    }},
    sensitivity: "aggregate-only public summary"
  }};

  const validationIndexPassFixture = {{
    schema_version: "scan-qc.public-safe-validation-index.v1",
    generated_at: "2026-05-11T01:12:00Z",
    status: "pass",
    summary: {{
      known_artifacts: 6,
      artifacts_present: 6,
      artifacts_passed: 6,
      artifacts_failed: 0,
      artifacts_missing: 0,
      unknown_inputs: 0,
      checks_passed: 12,
      checks_failed: 0,
      blocking_item_count: 0
    }},
    checks_passed: 12,
    checks_failed: 0,
    artifact_presence: {{
      "frontend_workbench_validation.json": {{ present: true, category: "frontend_workbench_validation", status: "pass", reported_status: "pass" }},
      "release_readiness_summary.json": {{ present: true, category: "release_readiness", status: "pass", reported_status: "pass" }},
      "release_candidate_summary.json": {{ present: true, category: "release_candidate", status: "pass", reported_status: "pass" }},
      "aggregate_evidence_bundle_summary.json": {{ present: true, category: "aggregate_evidence_bundle", status: "pass", reported_status: "pass" }},
      "review_decision_verification_summary.json": {{ present: true, category: "review_decision_verification", status: "pass", reported_status: "pass" }},
      "final_production_handoff_summary.json": {{ present: true, category: "final_production_handoff", status: "pass", reported_status: "pass" }}
    }},
    artifacts: {{
      "review_decision_verification_summary.json": {{
        present: true,
        status: "pass",
        reported_status: "pass",
        checks_passed: 3,
        checks_failed: 0,
        counts: {{ blocking_count: 0, warning_count: 1, blocking_counts_by_code: {{}}, warning_counts_by_code: {{ ignored_extra_decision_field: 1 }} }},
        privacy: {{ status: "pass", failure_count: 0, failure_codes: [] }},
        local_id: "PRIVATE-LOCAL-ID",
        source_type: "review_decision_export",
        source_schema: "scan-qc-review-decisions.local.v1"
      }},
      "aggregate_evidence_bundle_summary.json": {{
        present: true,
        status: "pass",
        review_decision_verification: {{
          present: true,
          status: "pass",
          checks_passed: 4,
          checks_failed: 0,
          blocking_count: 0,
          warning_count: 0,
          blocking_counts_by_code: {{}},
          warning_counts_by_code: {{}},
          privacy_status: "pass"
        }}
      }},
      "final_production_handoff_summary.json": {{
        present: true,
        status: "pass",
        review_decision_verification: {{
          present: true,
          status: "pass",
          checks_passed: 5,
          checks_failed: 0,
          blocking_count: 0,
          warning_count: 0,
          blocking_counts_by_code: {{}},
          warning_counts_by_code: {{}},
          privacy_status: "pass"
        }}
      }}
    }},
    blocking_items: [],
    privacy: {{
      aggregate_only: true,
      redacts_private_values: true,
      private_indicators_found: false,
      private_indicator_count: 0
    }},
    sensitive_values_omitted: true
  }};

  const validationIndexBlockedFixture = {{
    schema_version: "scan-qc.public-safe-validation-index.v1",
    generated_at: "2026-05-11T01:14:00Z",
    status: "fail",
    summary: {{
      known_artifacts: 5,
      artifacts_present: 3,
      artifacts_passed: 2,
      artifacts_failed: 1,
      artifacts_missing: 2,
      unknown_inputs: 1,
      checks_passed: 7,
      checks_failed: 4,
      blocking_item_count: 3
    }},
    checks_passed: 7,
    checks_failed: 4,
    artifact_presence: {{
      "frontend_workbench_validation.json": {{ present: true, category: "frontend_workbench_validation", status: "pass", reported_status: "pass" }},
      "release_readiness_summary.json": {{ present: true, category: "release_readiness", status: "fail", reported_status: "fail" }},
      "release_candidate_summary.json": {{ present: false, category: "release_candidate", status: "missing", reported_status: null }},
      "aggregate_evidence_bundle_summary.json": {{ present: true, category: "aggregate_evidence_bundle", status: "pass", reported_status: "pass" }},
      "final_production_handoff_summary.json": {{ present: false, category: "final_production_handoff", status: "missing", reported_status: null }}
    }},
    blocking_items: [
      {{ category: "release_readiness", code: "artifact_status_failed" }},
      {{ category: "release_candidate", code: "aggregate_artifact_missing" }},
      {{ category: "unknown", code: "unknown_public_safe_artifact" }}
    ],
    privacy: {{
      aggregate_only: true,
      redacts_private_values: true,
      private_indicators_found: false,
      private_indicator_count: 0
    }},
    sensitive_values_omitted: true
  }};

  const missingChecklistFixture = {{
    schema_version: "scan-qc-artifact-readiness-checklist.v1",
    generated_at: "2026-05-11T00:00:00Z",
    status: "fail",
    privacy: {{
      aggregate_only: true,
      redacts_private_values: true,
      private_indicators_found: false,
      private_indicator_count: 0
    }},
    sensitivity: "aggregate-only public summary",
    artifact_readiness_checklist: [
      {{
        artifact: "run_plan_summary.json",
        present: true,
        status: "pass",
        blocking_count: 0,
        warning_count: 1,
        privacy_status: "public-safe",
        generated_at: "2026-05-11T00:00:00Z"
      }},
      {{
        artifact: "review_summary.json",
        present: true,
        status: "stale",
        blocking_count: 1,
        warning_count: 2,
        privacy_status: "public-safe",
        generated_at: "2026-05-10T00:00:00Z"
      }},
      {{
        artifact: "acceptance_summary.json",
        present: false,
        status: "missing",
        blocking_count: 1,
        warning_count: 0,
        privacy_status: "not evaluated"
      }}
    ]
  }};

  const unsupportedSchemaFixture = {{
    schema_version: "scan-qc-artifact-readiness-checklist.v99",
    generated_at: "2026-05-11T00:00:00Z",
    status: "pass",
    privacy: {{
      aggregate_only: true,
      redacts_private_values: true,
      private_indicators_found: false,
      private_indicator_count: 0
    }},
    sensitivity: "aggregate-only public summary",
    artifact_readiness_checklist: {{
      "run_plan_summary.json": {{
        present: true,
        status: "pass",
        blocking_count: 0,
        warning_count: 0,
        privacy_status: "public-safe",
        generated_at: "2026-05-11T00:00:00Z"
      }}
    }}
  }};

  const failingPrivacyFixture = {{
    schema_version: "scan-qc-acceptance-summary.v1",
    generated_at: "2026-05-11T00:00:00Z",
    status: "fail",
    acceptance_passed: false,
    blocking_item_count: 1,
    blocking_items: [{{ code: "aggregate_privacy_hold" }}],
    warnings: [{{ code: "aggregate_warning_review_backlog" }}],
    privacy: {{
      aggregate_only: true,
      redacts_private_values: false,
      private_indicators_found: true,
      private_indicator_count: 1,
      contains_paths: true
    }},
    privacy_self_check: {{
      status: "failed",
      violation_count: 1
    }},
    sensitivity: "aggregate-only public summary"
  }};

  const missingPrivacyFixture = {{
    schema_version: "scan-qc-review-summary.v1",
    generated_at: "2026-05-11T00:00:00Z",
    status: "pass",
    status_counts: {{
      accepted_issue: 1
    }}
  }};

  const providerProbeFixture = {{
    schema_version: "scan-qc-provider-capability-probe-summary.v1",
    generated_at: "2026-05-11T01:20:00Z",
    status: "blocked",
    blocking_item_count: 0,
    blocking_items: [],
    warnings: [
      {{ code: "provider_probe_optional_packages_not_installed" }},
      {{ code: "provider_probe_gpu_not_visible" }}
    ],
    capability_probe_summary: {{
      provider_count: 3,
      configured_provider_count: 0,
      disabled_provider_count: 3,
      visible_gpu_count: 0,
      visible_model_count: 0,
      optional_package_visible_count: 1,
      optional_package_missing_count: 2,
      privacy_status: "public-safe",
      providers_configured: false
    }},
    privacy: {{
      aggregate_only: true,
      omits: [
        "source location strings",
        "source file identifiers",
        "content hashes",
        "recognized text",
        "thumbnails",
        "image content",
        "row-level findings"
      ],
      contains_paths: false,
      contains_filenames: false,
      contains_hashes: false,
      contains_ocr_text: false,
      contains_thumbnails: false,
      contains_image_content: false,
      contains_row_level_findings: false,
      redacts_private_values: true,
      private_indicators_found: false,
      private_indicator_count: 0
    }},
    privacy_self_check: {{
      status: "passed",
      violation_count: 0
    }},
    sensitivity: "aggregate-only public summary"
  }};

  const processingReviewFixture = {{
    schema_version: "scan-qc.processing-review.v1",
    generated_at: "2026-05-11T01:30:00Z",
    sensitivity: "local-only processing review summary; private row values omitted from this workbench view",
    privacy: {{
      local_only: true,
      aggregate_only: false,
      contains_row_level_paths: true,
      contains_hashes: true,
      contains_image_links: true,
      embeds_image_data: false,
      public_evidence: false
    }},
    summary: {{
      total_files: 6,
      processed_files: 4,
      failed_files: 1,
      skipped_files: 1,
      processing_resumed_files: 2,
      processing_duplicate_reused_files: 3,
      processing_existing_derivative_reused_files: 4,
      guardrail_warning_files: 2,
      status_counts: {{
        processed: 4,
        failed: 1,
        skipped: 1
      }},
      deskewed_files: 2,
      dark_border_trimmed_files: 1,
      cropped_files: 1,
      despeckled_files: 3
    }},
    groups: {{
      failed: {{ count: 1 }},
      guardrail_warnings: {{ count: 2 }}
    }}
  }};

  const workbenchPublicPassFixture = {{
    schema_version: "scan-qc.workbench-public-summary.v1",
    generated_at: "2026-05-11T02:00:00Z",
    status: "pass",
    ready: true,
    workflow_state: {{
      acceptance_status: "pass",
      deep_inspection_status: "pass",
      handoff_status: "pass",
      release_candidate_status: "pass",
      release_readiness_status: "pass",
      validation_index_status: "pass"
    }},
    checks_passed: 14,
    checks_failed: 0,
    blocking_item_count: 0,
    warning_item_count: 1,
    blocking_items: [],
    warning_items: [{{ code: "aggregate_warning_review_backlog" }}],
    blocking_counts_by_code: {{}},
    warning_counts_by_code: {{ aggregate_warning_review_backlog: 1 }},
    summary: {{
      known_artifacts: 12,
      artifacts_present: 9,
      artifacts_passed: 9,
      artifacts_failed: 0,
      artifacts_missing: 0,
      unsupported_inputs: 0,
      processing_resumed_files: 2,
      processing_duplicate_reused_files: 3,
      processing_existing_derivative_reused_files: 4,
      human_review_closure: {{
        review_present: true,
        review_status: "pass",
        acceptance_present: true,
        acceptance_status: "pass",
        total_findings: 9,
        remaining_p0: 0,
        remaining_p1: 2,
        status_counts: {{
          accepted_issue: 4,
          false_positive: 5
        }},
        severity_status_counts: {{
          P0: {{ accepted_issue: 0, false_positive: 0 }},
          P1: {{ accepted_issue: 4, false_positive: 1 }},
          P2: {{ accepted_issue: 0, false_positive: 4 }}
        }},
        acceptance_passed: true,
        acceptance_pass: true,
        privacy_status: "aggregate_public_safe"
      }},
      deep_inspection_readiness: {{
        status: "pass",
        candidate_total: 3,
        candidates_by_reason: {{ processing_review_status_failed: 1, quality_scanline_candidate: 2 }},
        candidates_by_severity: {{ P1: 1, P2: 2 }},
        provider_configured: false,
        provider_count: 0,
        checks_passed: 5,
        checks_failed: 0,
        privacy_status: "aggregate_public_safe",
        no_inference_run: true
      }},
      provider_capability_readiness: {{
        provider_count: 3,
        configured_provider_count: 0,
        disabled_provider_count: 3,
        providers_configured: false,
        provider_packages_found_count: 1,
        optional_package_visible_count: 1,
        optional_package_missing_count: 2,
        visible_gpu_count: 0,
        visible_model_count: 0,
        gpu_acceleration_configured: false,
        model_acceleration_configured: false,
        privacy_status: "aggregate_public_safe",
        no_inference_run: true,
        configuration_status: "not_configured"
      }},
      processing_operation_timings: {{
        auto_crop: {{
          enabled: true,
          file_count: 7,
          elapsed_seconds: 0.5,
          files_per_minute: 840,
          average_seconds_per_file: 0.071429
        }},
        deskew: {{
          enabled: true,
          file_count: 7,
          elapsed_seconds: 3.5,
          files_per_minute: 120,
          average_seconds_per_file: 0.5
        }},
        trim_dark_border: {{
          enabled: false,
          file_count: 0,
          elapsed_seconds: 0,
          files_per_minute: 0
        }},
        despeckle: {{
          enabled: true,
          file_count: 7,
          elapsed_seconds: 1.25,
          files_per_minute: 336,
          average_seconds_per_file: 0.178571,
          backend_mode: "numpy",
          numpy_available: true,
          backend_counts: {{
            numpy: 7,
            fallback: 0
          }}
        }}
      }}
    }},
    artifact_presence: completeChecklistFixture.artifact_readiness_checklist,
    privacy: {{
      aggregate_only: true,
      redacts_private_values: true,
      private_indicators_found: false,
      private_indicator_count: 0
    }},
    privacy_self_check: {{
      status: "passed",
      violation_count: 0
    }},
    sensitive_values_omitted: true,
    sensitivity: "aggregate-only public summary"
  }};

  const workbenchPublicBlockedFixture = {{
    schema_version: "scan-qc.workbench-public-summary.v1",
    generated_at: "2026-05-11T02:10:00Z",
    status: "fail",
    ready: false,
    workflow_state: {{
      acceptance_status: "fail",
      deep_inspection_status: "pass",
      handoff_status: null,
      release_candidate_status: null,
      release_readiness_status: null,
      validation_index_status: "fail"
    }},
    checks_passed: 11,
    checks_failed: 2,
    blocking_item_count: 3,
    warning_item_count: 2,
    blocking_items: [
      {{ code: "aggregate_artifact_missing" }},
      {{ code: "unsupported_input" }}
    ],
    warning_items: [{{ code: "aggregate_warning_review_backlog" }}],
    blocking_counts_by_code: {{
      aggregate_artifact_missing: 2,
      unsupported_input: 1
    }},
    warning_counts_by_code: {{
      aggregate_warning_review_backlog: 2
    }},
    summary: {{
      known_artifacts: 12,
      artifacts_present: 6,
      artifacts_passed: 5,
      artifacts_failed: 1,
      artifacts_missing: 2,
      unsupported_inputs: 1
    }},
    artifact_presence: {{
      ...completeChecklistFixture.artifact_readiness_checklist,
      "acceptance_summary.json": {{ present: true, status: "fail", blocking_count: 1, warning_count: 1, privacy_status: "public-safe", generated_at: "2026-05-11T00:02:00Z" }},
      "release_candidate_summary.json": {{ present: false, status: "missing", blocking_count: 1, warning_count: 0, privacy_status: "not evaluated" }}
    }},
    unsupported_inputs: {{
      label: "unsupported_input",
      count: 1,
      aggregate_code: "unsupported_input"
    }},
    privacy: {{
      aggregate_only: true,
      redacts_private_values: true,
      private_indicators_found: false,
      private_indicator_count: 0
    }},
    privacy_self_check: {{
      status: "passed",
      violation_count: 0
    }},
    sensitive_values_omitted: true,
    sensitivity: "aggregate-only public summary"
  }};

  const reviewDecisionVerificationPassFixture = {{
    schema_version: "scan-qc.review-decision-verification-summary.v1",
    generated_at: "2026-05-11T02:12:00Z",
    status: "pass",
    checks_passed: 1,
    checks_failed: 0,
    source: {{
      schema: "scan-qc-review-decisions.local.v1",
      source_type: "aggregate-handoff"
    }},
    decision_summary: {{
      total_decisions: 18,
      pending: 0,
      accepted: 12,
      rejected: 4,
      rework: 2,
      completion_status: "complete",
      decision_counts: {{
        pending: 0,
        accepted_issue: 12,
        false_positive: 4,
        fixed_externally: 1,
        needs_rescan: 1,
        blocked: 0
      }}
    }},
    blocking_count: 0,
    warning_count: 1,
    blocking_counts_by_code: {{}},
    warning_counts_by_code: {{ aggregate_warning_review_backlog: 1 }},
    privacy: {{
      status: "pass",
      aggregate_only: true,
      sensitive_field_count: 0,
      source_values_omitted: true
    }},
    sensitive_values_omitted: true,
    sensitivity: "aggregate-only public summary"
  }};

  const reviewDecisionVerificationBlockedFixture = {{
    schema_version: "scan-qc.review-decision-verification-summary.v1",
    generated_at: "2026-05-11T02:13:00Z",
    status: "blocked",
    checks_passed: 6,
    checks_failed: 2,
    source: {{
      schema: "scan-qc-review-decisions.local.v1",
      source_type: "aggregate-handoff"
    }},
    decision_summary: {{
      total_decisions: 18,
      pending: 3,
      accepted: 10,
      rejected: 3,
      rework: 2,
      completion_status: "incomplete",
      decision_counts: {{
        pending: 3,
        accepted_issue: 10,
        false_positive: 3,
        fixed_externally: 1,
        needs_rescan: 1,
        blocked: 0
      }}
    }},
    blocking_count: 2,
    warning_count: 1,
    blocking_counts_by_code: {{
      review_decision_pending: 1,
      review_decision_rework_unresolved: 1
    }},
    warning_counts_by_code: {{ aggregate_warning_review_backlog: 1 }},
    privacy_status: "public-safe",
    privacy: {{
      status: "pass",
      aggregate_only: true,
      sensitive_field_count: 0,
      source_values_omitted: true
    }},
    sensitive_values_omitted: true,
    sensitivity: "aggregate-only public summary"
  }};

  const workbenchPublicPassModel = inferArtifact(workbenchPublicPassFixture);
  assert(workbenchPublicPassModel.sourceType === "aggregate-handoff", "workbench public pass fixture did not load as aggregate handoff");
  assert(workbenchPublicPassModel.aggregateHandoff.artifactType === "Workbench public summary", "workbench public pass fixture did not classify as workbench public summary");
  assert(workbenchPublicPassModel.aggregateHandoff.status === "pass", "workbench public pass status was not pass");
  assert(workbenchPublicPassModel.aggregateHandoff.readyFlag === true, "workbench public pass ready flag was not true");
  assert(workbenchPublicPassModel.aggregateHandoff.workflowState.includes("acceptance_status: pass"), "workbench public pass workflow state was not preserved");
  assert(workbenchPublicPassModel.aggregateHandoff.checksPassed === 14, "workbench public pass checks passed were not preserved");
  assert(workbenchPublicPassModel.aggregateHandoff.checksFailed === 0, "workbench public pass checks failed were not preserved");
  assert(workbenchPublicPassModel.aggregateHandoff.validationIndex.artifactsPresent === 9, "workbench public pass artifact present count was not preserved");
  assert(workbenchPublicPassModel.aggregateHandoff.deepInspectionCandidate.candidateTotal === 3, "workbench public pass promoted deep-inspection candidate total was not preserved");
  assert(workbenchPublicPassModel.aggregateHandoff.deepInspectionCandidate.providerConfigured === false, "workbench public pass promoted provider configured flag was not preserved");
  assert(countFor(workbenchPublicPassModel.aggregateHandoff.deepInspectionCandidate.candidatesBySeverity, "P1") === 1, "workbench public pass promoted candidate severity count was not preserved");
  assert(workbenchPublicPassModel.aggregateHandoff.providerProbe.providerCount === 3, "workbench public pass promoted provider count was not preserved");
  assert(workbenchPublicPassModel.aggregateHandoff.providerProbe.providersConfigured === false, "workbench public pass promoted provider configured status was not preserved");
  assert(workbenchPublicPassModel.aggregateHandoff.providerProbe.optionalPackageVisibleCount === 1, "workbench public pass promoted optional package count was not preserved");
  assert(workbenchPublicPassModel.aggregateHandoff.counts.processingResumedFiles === 2, "workbench public pass processing resumed count was not preserved");
  assert(workbenchPublicPassModel.aggregateHandoff.counts.processingDuplicateReusedFiles === 3, "workbench public pass duplicate reuse count was not preserved");
  assert(workbenchPublicPassModel.aggregateHandoff.counts.processingExistingDerivativeReusedFiles === 4, "workbench public pass existing derivative reuse count was not preserved");
  assert(workbenchPublicPassModel.aggregateHandoff.processingOperationTimings.length === 4, "workbench public pass operation timings were not preserved");
  assert(workbenchPublicPassModel.aggregateHandoff.processingOperationTimings[0].operation === "deskew", "workbench public pass operation timings were not sorted by elapsed hotspot");
  assert(workbenchPublicPassModel.aggregateHandoff.processingOperationTimings[0].elapsedSeconds === 3.5, "workbench public pass deskew elapsed seconds were not preserved");
  assert(workbenchPublicPassModel.aggregateHandoff.processingOperationTimings[1].operation === "despeckle", "workbench public pass despeckle hotspot order was not preserved");
  assert(workbenchPublicPassModel.aggregateHandoff.processingOperationTimings[1].averageSecondsPerFile === 0.178571, "workbench public pass average seconds per file was not preserved");
  assert(workbenchPublicPassModel.aggregateHandoff.despeckleBackend.backendMode === "numpy", "workbench public pass despeckle backend mode was not preserved");
  assert(workbenchPublicPassModel.aggregateHandoff.despeckleBackend.numpyAvailable === true, "workbench public pass despeckle numpy availability was not preserved");
  assert(countFor(workbenchPublicPassModel.aggregateHandoff.despeckleBackend.backendCounts, "numpy") === 7, "workbench public pass despeckle numpy backend count was not preserved");
  assert(countFor(workbenchPublicPassModel.aggregateHandoff.despeckleBackend.backendCounts, "fallback") === 0, "workbench public pass despeckle fallback backend count was not preserved");
  assert(workbenchPublicPassModel.aggregateHandoff.remainingP0 === 0, "workbench public pass did not read promoted remaining P0");
  assert(workbenchPublicPassModel.aggregateHandoff.remainingP1 === 2, "workbench public pass did not read promoted remaining P1");
  assert(workbenchPublicPassModel.aggregateHandoff.acceptancePassed === true, "workbench public pass did not read promoted acceptance signal");
  assert(countFor(workbenchPublicPassModel.aggregateHandoff.reviewStatusCounts, "false_positive") === 5, "workbench public pass did not read promoted review status counts");
  assert(workbenchPublicPassModel.aggregateHandoff.warningCodeCounts[0].name === "aggregate_warning_review_backlog", "workbench public pass warning code count label was not preserved");
  assert(workbenchPublicPassModel.artifactCompatibility.schemaRecognized === true, "workbench public pass schema was not recognized");
  assert(!workbenchPublicPassModel.artifactCompatibility.diagnostics.some(item => item.code === "aggregate_status_fields_missing"), "workbench public pass reported missing aggregate status fields");
  state.model = workbenchPublicPassModel;
  renderAggregateHandoff();
  assert(els.aggregateHandoff.innerHTML.includes("工作台公开摘要"), "workbench public pass did not render artifact type");
  assert(els.aggregateHandoff.innerHTML.includes("工作流状态"), "workbench public pass did not render workflow state label");
  assert(els.aggregateHandoff.innerHTML.includes("acceptance_status: pass"), "workbench public pass did not render workflow state value");
  assert(!els.aggregateHandoff.innerHTML.includes("[object Object]"), "workbench public pass rendered workflow object directly");
  assert(els.aggregateHandoff.innerHTML.includes("workbench_public_summary.json"), "workbench public pass did not render known artifact card");
  assert(els.aggregateHandoff.innerHTML.includes("处理已恢复"), "workbench public pass did not render resumed processing count");
  assert(els.aggregateHandoff.innerHTML.includes("处理重复复用"), "workbench public pass did not render duplicate reuse count");
  assert(els.aggregateHandoff.innerHTML.includes("处理已有衍生复用"), "workbench public pass did not render existing derivative reuse count");
  assert(els.aggregateHandoff.innerHTML.includes("处理操作耗时热点"), "workbench public pass did not render operation timing hotspots");
  assert(els.aggregateHandoff.innerHTML.indexOf("纠偏") < els.aggregateHandoff.innerHTML.indexOf("去噪点"), "workbench public pass did not render largest elapsed operation first");
  assert(els.aggregateHandoff.innerHTML.includes("耗时秒数"), "workbench public pass did not render elapsed seconds column");
  assert(els.aggregateHandoff.innerHTML.includes("平均秒/文件"), "workbench public pass did not render per-file average column");
  assert(els.aggregateHandoff.innerHTML.includes("去噪点后端摘要"), "workbench public pass did not render despeckle backend summary");
  assert(els.aggregateHandoff.innerHTML.includes("深度检查候选摘要"), "workbench public pass did not render promoted deep-inspection readiness");
  assert(els.aggregateHandoff.innerHTML.includes("提供方能力探测"), "workbench public pass did not render promoted provider readiness");
  assert(els.aggregateHandoff.innerHTML.includes("可见可选包数"), "workbench public pass did not render promoted optional package count");
  assert(els.aggregateHandoff.innerHTML.includes("后端模式"), "workbench public pass did not render despeckle backend mode label");
  assert(els.aggregateHandoff.innerHTML.includes("numpy"), "workbench public pass did not render despeckle backend mode value");
  assert(els.aggregateHandoff.innerHTML.includes("NumPy 可用"), "workbench public pass did not render numpy availability label");
  assert(els.aggregateHandoff.innerHTML.includes("后端计数"), "workbench public pass did not render despeckle backend counts");
  assert(els.aggregateHandoff.innerHTML.includes("警告代码"), "workbench public pass did not render warning code counts");
  assert(els.aggregateHandoff.innerHTML.includes("剩余 P0"), "workbench public pass did not render promoted remaining P0");
  assert(els.aggregateHandoff.innerHTML.includes("复核状态计数"), "workbench public pass did not render promoted review status counts");
  assert(els.aggregateHandoff.innerHTML.includes("验收通过"), "workbench public pass did not render promoted acceptance signal");
  assertPublicSafe(els.aggregateHandoff.innerHTML, "workbench public pass rendering");

  const workbenchPublicBlockedModel = inferArtifact(workbenchPublicBlockedFixture);
  assert(workbenchPublicBlockedModel.sourceType === "aggregate-handoff", "workbench public blocked fixture did not load as aggregate handoff");
  assert(workbenchPublicBlockedModel.aggregateHandoff.artifactType === "Workbench public summary", "workbench public blocked fixture did not classify as workbench public summary");
  assert(workbenchPublicBlockedModel.aggregateHandoff.status === "fail", "workbench public blocked status was not fail");
  assert(workbenchPublicBlockedModel.aggregateHandoff.readyFlag === false, "workbench public blocked ready flag was not false");
  assert(workbenchPublicBlockedModel.aggregateHandoff.workflowState.includes("acceptance_status: fail"), "workbench public blocked workflow state was not preserved");
  assert(workbenchPublicBlockedModel.aggregateHandoff.blockingItemCount === 3, "workbench public blocked blocking count was not preserved");
  assert(countFor(workbenchPublicBlockedModel.aggregateHandoff.blockingCodeCounts, "unsupported_input") === 1, "workbench public blocked unsupported input count was not preserved");
  assert(workbenchPublicBlockedModel.aggregateHandoff.validationIndex.unknownInputs === 1, "workbench public blocked unsupported input aggregate count was not preserved");
  assert(workbenchPublicBlockedModel.aggregateHandoff.despeckleBackend.backendMode === "未提供", "workbench public blocked missing despeckle backend mode did not stay non-blocking");
  assert(workbenchPublicBlockedModel.aggregateHandoff.despeckleBackend.numpyAvailable === null, "workbench public blocked missing numpy availability did not stay non-blocking");
  assert(workbenchPublicBlockedModel.aggregateHandoff.despeckleBackend.backendCounts.length === 0, "workbench public blocked missing backend counts did not stay non-blocking");
  assert(!workbenchPublicBlockedModel.artifactCompatibility.diagnostics.some(item => item.code === "aggregate_status_fields_missing"), "workbench public blocked reported missing aggregate status fields");
  state.model = workbenchPublicBlockedModel;
  renderAggregateHandoff();
  assert(els.aggregateHandoff.innerHTML.includes("unsupported_input"), "workbench public blocked did not render unsupported input aggregate code");
  assert(els.aggregateHandoff.innerHTML.includes("阻塞代码"), "workbench public blocked did not render blocking code counts");
  assert(els.aggregateHandoff.innerHTML.includes("acceptance_status: fail"), "workbench public blocked did not render workflow state");
  assert(!els.aggregateHandoff.innerHTML.includes("[object Object]"), "workbench public blocked rendered workflow object directly");
  assert(els.aggregateHandoff.innerHTML.includes("工件缺失数"), "workbench public blocked did not render missing artifact count");
  assert(els.aggregateHandoff.innerHTML.includes("未提供"), "workbench public blocked did not render absent despeckle backend fields as unknown");
  assert(!els.aggregateHandoff.innerHTML.includes("unsupported_inputs"), "workbench public blocked rendered unsupported input object details");
  assertPublicSafe(els.aggregateHandoff.innerHTML, "workbench public blocked rendering");

  const reviewDecisionVerificationPassModel = inferArtifact(reviewDecisionVerificationPassFixture);
  assert(reviewDecisionVerificationPassModel.sourceType === "aggregate-handoff", "review decision verification pass fixture did not load as aggregate handoff");
  assert(reviewDecisionVerificationPassModel.aggregateHandoff.artifactType === "Review decision verification summary", "review decision verification pass fixture did not classify correctly");
  assert(reviewDecisionVerificationPassModel.aggregateHandoff.status === "pass", "review decision verification pass status was not pass");
  assert(reviewDecisionVerificationPassModel.aggregateHandoff.reviewDecisionVerification.checksPassed === 1, "review decision verification pass checks passed were not preserved");
  assert(reviewDecisionVerificationPassModel.aggregateHandoff.reviewDecisionVerification.totalDecisions === 18, "review decision verification pass total decisions were not preserved");
  assert(reviewDecisionVerificationPassModel.aggregateHandoff.reviewDecisionVerification.pendingCount === 0, "review decision verification pass pending count was not preserved");
  assert(reviewDecisionVerificationPassModel.aggregateHandoff.reviewDecisionVerification.acceptedCount === 12, "review decision verification pass accepted count was not preserved");
  assert(reviewDecisionVerificationPassModel.aggregateHandoff.reviewDecisionVerification.rejectedCount === 4, "review decision verification pass rejected count was not preserved");
  assert(reviewDecisionVerificationPassModel.aggregateHandoff.reviewDecisionVerification.reworkCount === 2, "review decision verification pass rework count was not preserved");
  assert(reviewDecisionVerificationPassModel.artifactCompatibility.schemaRecognized === true, "review decision verification pass schema was not recognized");
  assert(!reviewDecisionVerificationPassModel.artifactCompatibility.diagnostics.some(item => item.code === "aggregate_status_fields_missing"), "review decision verification pass reported missing aggregate status fields");
  state.model = reviewDecisionVerificationPassModel;
  renderAggregateHandoff();
  assert(els.aggregateHandoff.innerHTML.includes("复核决定验证摘要"), "review decision verification pass did not render summary panel");
  assert(els.aggregateHandoff.innerHTML.includes("决定总数"), "review decision verification pass did not render total decisions");
  assert(els.aggregateHandoff.innerHTML.includes("隐私状态"), "review decision verification pass did not render privacy status");
  assert(!els.aggregateHandoff.innerHTML.includes("source_field"), "review decision verification pass rendered source field details");
  assert(!els.aggregateHandoff.innerHTML.includes("source_type"), "review decision verification pass rendered source type details");
  assert(!els.aggregateHandoff.innerHTML.includes("scan-qc-review-decisions.local.v1"), "review decision verification pass rendered source schema details");
  assertPublicSafe(els.aggregateHandoff.innerHTML, "review decision verification pass rendering");

  const reviewDecisionVerificationBlockedModel = inferArtifact(reviewDecisionVerificationBlockedFixture);
  assert(reviewDecisionVerificationBlockedModel.sourceType === "aggregate-handoff", "review decision verification blocked fixture did not load as aggregate handoff");
  assert(reviewDecisionVerificationBlockedModel.aggregateHandoff.artifactType === "Review decision verification summary", "review decision verification blocked fixture did not classify correctly");
  assert(reviewDecisionVerificationBlockedModel.aggregateHandoff.status === "fail", "review decision verification blocked status was not normalized to fail");
  assert(reviewDecisionVerificationBlockedModel.aggregateHandoff.reviewDecisionVerification.checksFailed === 2, "review decision verification blocked checks failed were not preserved");
  assert(reviewDecisionVerificationBlockedModel.aggregateHandoff.reviewDecisionVerification.pendingCount === 3, "review decision verification blocked pending count was not preserved");
  assert(reviewDecisionVerificationBlockedModel.aggregateHandoff.reviewDecisionVerification.blockingCount === 2, "review decision verification blocked blocking count was not preserved");
  assert(countFor(reviewDecisionVerificationBlockedModel.aggregateHandoff.reviewDecisionVerification.blockingCodeCounts, "review_decision_pending") === 1, "review decision verification blocked code count was not preserved");
  assert(!reviewDecisionVerificationBlockedModel.artifactCompatibility.diagnostics.some(item => item.code === "aggregate_status_fields_missing"), "review decision verification blocked reported missing aggregate status fields");
  state.model = reviewDecisionVerificationBlockedModel;
  renderAggregateHandoff();
  assert(els.aggregateHandoff.innerHTML.includes("review_decision_pending"), "review decision verification blocked did not render blocking code count");
  assert(els.aggregateHandoff.innerHTML.includes("按代码统计警告"), "review decision verification blocked did not render warning code counts");
  assert(!els.aggregateHandoff.innerHTML.includes("local_id"), "review decision verification blocked rendered local ID details");
  assert(!els.aggregateHandoff.innerHTML.includes("source_type"), "review decision verification blocked rendered source type details");
  assert(!els.aggregateHandoff.innerHTML.includes("scan-qc-review-decisions.local.v1"), "review decision verification blocked rendered source schema details");
  assertPublicSafe(els.aggregateHandoff.innerHTML, "review decision verification blocked rendering");

  const actualGeneratedWorkbenchModel = inferArtifact(actualGeneratedWorkbenchFixture);
  assert(actualGeneratedWorkbenchModel.sourceType === "aggregate-handoff", "actual generated workbench summary did not load as aggregate handoff");
  assert(actualGeneratedWorkbenchModel.aggregateHandoff.artifactType === "Workbench public summary", "actual generated workbench summary did not classify as workbench public summary");
  assert(actualGeneratedWorkbenchModel.aggregateHandoff.status === "fail", "actual generated workbench summary status was not fail");
  assert(actualGeneratedWorkbenchModel.aggregateHandoff.validationIndex.unknownInputs === 1, "actual generated workbench summary unsupported input count was not preserved");
  assert(countFor(actualGeneratedWorkbenchModel.aggregateHandoff.blockingCodeCounts, "unsupported_private_looking_input_rejected") === 1, "actual generated workbench summary unsupported aggregate code was not preserved");
  assert(!actualGeneratedWorkbenchModel.artifactCompatibility.diagnostics.some(item => item.code === "aggregate_status_fields_missing"), "actual generated workbench summary reported missing aggregate status fields");
  state.model = actualGeneratedWorkbenchModel;
  renderAggregateHandoff();
  assert(els.aggregateHandoff.innerHTML.includes("unsupported_private_looking_input_rejected"), "actual generated workbench summary did not render unsupported aggregate code");
  assert(!els.aggregateHandoff.innerHTML.includes("[object Object]"), "actual generated workbench summary rendered workflow object directly");
  assert(!els.aggregateHandoff.innerHTML.includes("private_scan_alpha.tif"), "actual generated workbench summary rendered private input basename");
  assert(!els.aggregateHandoff.innerHTML.includes("PRIVATE_OCR_TEXT"), "actual generated workbench summary rendered private OCR-like content");
  assertPublicSafe(els.aggregateHandoff.innerHTML, "actual generated workbench summary rendering");

  const reviewModel = inferArtifact(reviewFixture);
  assert(reviewModel.sourceType === "aggregate-handoff", "review fixture did not load as aggregate handoff");
  assert(reviewModel.aggregateHandoff.artifactType === "Review summary", "review fixture did not classify as Review summary");
  assert(reviewModel.aggregateHandoff.status === "pass", "review fixture status was not pass");
  assert(countFor(reviewModel.aggregateHandoff.reviewStatusCounts, "false_positive") === 5, "review fixture status counts were not preserved");
  state.model = reviewModel;
  renderAggregateHandoff();
  assert(els.aggregateHandoff.innerHTML.includes("复核摘要"), "review fixture did not render Review summary");
  assert(els.aggregateHandoff.innerHTML.includes("复核状态计数"), "review fixture did not render review status counts");
  assert(els.aggregateHandoff.innerHTML.includes("公开安全工件兼容性诊断"), "review fixture did not render compatibility diagnostics");
  assert(els.aggregateHandoff.innerHTML.includes("artifact_compatibility_pass"), "review fixture did not render compatibility pass code");
  assert(els.aggregateHandoff.innerHTML.includes("架构/类型检测"), "review fixture did not render schema/type detection");

  const acceptanceModel = inferArtifact(acceptanceFixture);
  assert(acceptanceModel.sourceType === "aggregate-handoff", "acceptance fixture did not load as aggregate handoff");
  assert(acceptanceModel.aggregateHandoff.artifactType === "Acceptance summary", "acceptance fixture did not classify as Acceptance summary");
  assert(acceptanceModel.aggregateHandoff.status === "pass", "acceptance fixture aggregate-only status was not pass");
  assert(acceptanceModel.aggregateHandoff.acceptancePassed === true, "acceptance fixture did not preserve acceptance_passed/pass");
  assert(acceptanceModel.aggregateHandoff.blockingItemCount === 0, "acceptance fixture blocking count was not aggregate-only zero");
  assert(acceptanceModel.aggregateHandoff.remainingP0 === 0, "acceptance fixture remaining P0 was not aggregate-only zero");
  assert(acceptanceModel.aggregateHandoff.remainingP1 === 2, "acceptance fixture remaining P1 was not preserved");
  assert(acceptanceModel.aggregateHandoff.warningCount === 2, "acceptance fixture warning count was not preserved");
  assert(acceptanceModel.aggregateHandoff.privacy.aggregateOnly === true, "acceptance fixture privacy was not aggregate-only");
  assert(acceptanceModel.aggregateHandoff.privacy.containsPaths === false, "acceptance fixture reported private paths");
  assert(acceptanceModel.aggregateHandoff.privacy.containsFilenames === false, "acceptance fixture reported private filenames");
  state.model = acceptanceModel;
  renderAggregateHandoff();
  assert(els.aggregateHandoff.innerHTML.includes("验收摘要"), "acceptance fixture did not render Acceptance summary");
  assert(els.aggregateHandoff.innerHTML.includes("aggregate_warning_review_backlog"), "acceptance fixture did not render warning code");
  assert(els.aggregateHandoff.innerHTML.includes("仅汇总状态"), "acceptance fixture did not render aggregate-only status");
  assert(els.aggregateHandoff.innerHTML.includes("验收通过"), "acceptance fixture did not render acceptance status");
  assert(els.aggregateHandoff.innerHTML.includes("隐私摘要"), "acceptance fixture did not render privacy summary diagnostic");

  const completeChecklistModel = inferArtifact(completeChecklistFixture);
  assert(completeChecklistModel.sourceType === "aggregate-handoff", "complete checklist fixture did not load as aggregate handoff");
  assert(completeChecklistModel.artifactReadiness.ready === true, "complete checklist fixture was not ready");
  assert(completeChecklistModel.artifactReadiness.missingCount === 0, "complete checklist fixture reported missing artifacts");
  assert(completeChecklistModel.artifactReadiness.rows.length === 11, "complete checklist fixture did not cover eleven expected artifacts");
  assert(completeChecklistModel.aggregateHandoff.status === "pass", "complete checklist fixture did not preserve aggregate pass status");
  assert(completeChecklistModel.aggregateHandoff.blockingItemCount === 0, "complete checklist fixture did not preserve aggregate blocking count");
  assert(completeChecklistModel.aggregateHandoff.warningCount === 0, "complete checklist fixture did not preserve aggregate warning count");
  assert(completeChecklistFixture.summary.required_missing_count === 0, "complete checklist fixture did not include generated required missing count");
  assert(completeChecklistFixture.aggregate_counts.blocking_count === 0, "complete checklist fixture did not include generated aggregate blocking count");
  assert(completeChecklistFixture.aggregate_counts.warning_count === 0, "complete checklist fixture did not include generated aggregate warning count");
  assert(completeChecklistFixture.artifact_readiness_checklist["workbench_public_summary.json"].category === "workbench_public_summary", "complete checklist fixture did not include generated workbench summary row category");
  assert(completeChecklistFixture.artifact_readiness_checklist["workbench_public_summary.json"].required === true, "complete checklist fixture did not include generated workbench summary required flag");
  assert(completeChecklistFixture.artifact_readiness_checklist["workbench_public_summary.json"].blocking_counts_by_code && Object.keys(completeChecklistFixture.artifact_readiness_checklist["workbench_public_summary.json"].blocking_counts_by_code).length === 0, "complete checklist fixture did not include generated row blocking code counts");
  state.model = completeChecklistModel;
  renderAggregateHandoff();
  assert(els.aggregateHandoff.innerHTML.includes("公开安全工件就绪清单"), "complete checklist did not render checklist heading");
  assert(els.aggregateHandoff.innerHTML.includes("公开交接就绪"), "complete checklist did not render ready summary");
  assert(els.aggregateHandoff.innerHTML.includes("final_production_handoff_summary.json"), "complete checklist did not render final handoff artifact");
  assert(els.aggregateHandoff.innerHTML.includes("deep_inspection_candidate_summary.json"), "complete checklist did not render deep-inspection candidate artifact");
  assert(els.aggregateHandoff.innerHTML.includes("review_decision_verification_summary.json"), "complete checklist did not render review decision verification artifact");
  assert(!els.aggregateHandoff.innerHTML.includes("blob:aggregate-fixture"), "complete checklist rendered object URL state");

  const frontendValidationPassFixture = {{
    status: "pass",
    validated_html_path: "docs/frontend-workbench-prototype.html",
    error_count: 0,
    errors: [],
    counts: {{ required_regions: 9, forbidden_pattern_checks: 8 }},
    coverage: {{ aggregate_summary: true, executable_fixtures: true, preview_lifecycle: true }},
    privacy: {{
      aggregate_only: true,
      forbidden_pattern_checks_passed: true,
      preview_lifecycle_public_safe: true,
      contains_paths: false,
      contains_filenames: false,
      contains_hashes: false,
      contains_ocr_text: false,
      contains_thumbnails: false,
      contains_image_content: false,
      contains_row_level_findings: false,
      redacts_private_values: true
    }},
    fixture_groups: {{ aggregate_executable_fixture_groups: 12, preview_lifecycle_synthetic_slots: 2 }}
  }};
  const frontendValidationBlockedFixture = {{
    schema_version: "scan-qc.frontend-workbench-validation.v1",
    status: "fail",
    validated_html_path: "/Users/example/private/frontend-workbench-prototype.html",
    error_count: 3,
    errors: [
      {{ code: "missing_required_region", message: "Missing required region." }},
      {{ code: "private_path_leak", message: "/Users/example/private/image.tif leaked in preview." }},
      {{ code: "private_filename_leak", message: "private_scan_alpha.tif failed frontend validation." }}
    ],
    counts: {{ required_regions: 9, forbidden_pattern_checks: 8 }},
    coverage: {{ aggregate_summary: true, executable_fixtures: false, preview_lifecycle: true }},
    privacy: {{
      aggregate_only: true,
      forbidden_pattern_checks_passed: false,
      preview_lifecycle_public_safe: true,
      contains_paths: false,
      contains_filenames: false,
      contains_hashes: false,
      contains_ocr_text: false,
      contains_thumbnails: false,
      contains_image_content: false,
      contains_row_level_findings: false,
      redacts_private_values: true
    }},
    fixture_groups: {{ aggregate_executable_fixture_groups: 12, preview_lifecycle_synthetic_slots: 2 }}
  }};
  const frontendValidationPassModel = inferArtifact(frontendValidationPassFixture);
  assert(frontendValidationPassModel.sourceType === "aggregate-handoff", "passing frontend validation fixture did not load as aggregate handoff");
  assert(frontendValidationPassModel.aggregateHandoff.artifactType === "Frontend workbench validation summary", "passing frontend validation fixture did not classify correctly");
  assert(frontendValidationPassModel.aggregateHandoff.frontendValidation.errorCount === 0, "passing frontend validation error count was not preserved");
  assert(frontendValidationPassModel.aggregateHandoff.frontendValidation.validatedHtmlPath === "docs/frontend-workbench-prototype.html", "passing frontend validation repo-relative path was not preserved");
  state.model = frontendValidationPassModel;
  renderAggregateHandoff();
  assert(els.aggregateHandoff.innerHTML.includes("前端工作台验证摘要"), "passing frontend validation summary did not render");
  assert(els.aggregateHandoff.innerHTML.includes("覆盖布尔项"), "passing frontend validation coverage booleans did not render");
  assert(els.aggregateHandoff.innerHTML.includes("隐私布尔项"), "passing frontend validation privacy booleans did not render");
  assert(els.aggregateHandoff.innerHTML.includes("required_regions"), "passing frontend validation count summary did not render");
  assertPublicSafe(els.aggregateHandoff.innerHTML, "passing frontend validation summary rendering");

  const frontendValidationBlockedModel = inferArtifact(frontendValidationBlockedFixture);
  assert(frontendValidationBlockedModel.sourceType === "aggregate-handoff", "blocked frontend validation fixture did not load as aggregate handoff");
  assert(frontendValidationBlockedModel.aggregateHandoff.artifactType === "Frontend workbench validation summary", "blocked frontend validation fixture did not classify correctly");
  assert(frontendValidationBlockedModel.aggregateHandoff.status === "fail", "blocked frontend validation status was not fail");
  assert(frontendValidationBlockedModel.aggregateHandoff.frontendValidation.errorCount === 3, "blocked frontend validation error count was not preserved");
  assert(frontendValidationBlockedModel.aggregateHandoff.frontendValidation.validatedHtmlPath.includes("redacted"), "blocked frontend validation absolute path was not redacted");
  state.model = frontendValidationBlockedModel;
  renderAggregateHandoff();
  assert(els.aggregateHandoff.innerHTML.includes("missing_required_region"), "blocked frontend validation safe error code did not render");
  assert(els.aggregateHandoff.innerHTML.includes("private_path_leak"), "blocked frontend validation second error code did not render");
  assert(els.aggregateHandoff.innerHTML.includes("private_filename_leak"), "blocked frontend validation bare filename error code did not render");
  assert(els.aggregateHandoff.innerHTML.includes("redacted private diagnostic"), "blocked frontend validation private error message was not redacted");
  assert(!els.aggregateHandoff.innerHTML.includes("/Users/example"), "blocked frontend validation rendered absolute path");
  assert(!els.aggregateHandoff.innerHTML.includes("image.tif"), "blocked frontend validation rendered private filename");
  assert(!els.aggregateHandoff.innerHTML.includes("private_scan_alpha.tif"), "blocked frontend validation rendered bare private filename");
  assertPublicSafe(els.aggregateHandoff.innerHTML, "blocked frontend validation summary rendering");

  const deepInspectionCandidateModel = inferArtifact(deepInspectionCandidateFixture);
  assert(deepInspectionCandidateModel.sourceType === "aggregate-handoff", "deep-inspection candidate fixture did not load as aggregate handoff");
  assert(deepInspectionCandidateModel.aggregateHandoff.artifactType === "Deep-inspection candidate summary", "deep-inspection candidate fixture did not classify as candidate summary");
  assert(deepInspectionCandidateModel.aggregateHandoff.deepInspectionCandidate.candidateTotal === 3, "deep-inspection candidate total was not preserved");
  assert(deepInspectionCandidateModel.aggregateHandoff.deepInspectionCandidate.providerConfigured === false, "deep-inspection provider configured flag was not preserved");
  assert(deepInspectionCandidateModel.aggregateHandoff.deepInspectionCandidate.providerCount === 0, "deep-inspection provider count was not preserved");
  assert(deepInspectionCandidateModel.aggregateHandoff.deepInspectionCandidate.checksPassed === 3, "deep-inspection checks passed count was not derived from codes");
  assert(deepInspectionCandidateModel.aggregateHandoff.deepInspectionCandidate.checksFailed === 0, "deep-inspection checks failed count was not preserved");
  assert(deepInspectionCandidateModel.aggregateHandoff.deepInspectionCandidate.privacyStatus === "aggregate_public_safe", "deep-inspection privacy status was not preserved");
  assert(deepInspectionCandidateModel.aggregateHandoff.deepInspectionCandidate.noInferenceRun === true, "deep-inspection no-inference flag was not preserved");
  state.model = deepInspectionCandidateModel;
  renderAggregateHandoff();
  assert(els.aggregateHandoff.innerHTML.includes("深度检查候选摘要"), "deep-inspection candidate panel did not render");
  assert(els.aggregateHandoff.innerHTML.includes("候选总数"), "deep-inspection candidate total did not render");
  assert(els.aggregateHandoff.innerHTML.includes("按原因统计候选"), "deep-inspection reason counts did not render");
  assert(els.aggregateHandoff.innerHTML.includes("quality_scanline_candidate"), "deep-inspection reason code did not render");
  assert(els.aggregateHandoff.innerHTML.includes("未运行推理"), "deep-inspection no-inference flag did not render");
  assertPublicSafe(els.aggregateHandoff.innerHTML, "direct deep-inspection candidate rendering");

  const aggregateEvidenceBundleCandidateModel = inferArtifact(aggregateEvidenceBundleCandidateFixture);
  assert(aggregateEvidenceBundleCandidateModel.sourceType === "aggregate-handoff", "aggregate evidence bundle candidate fixture did not load as aggregate handoff");
  assert(aggregateEvidenceBundleCandidateModel.aggregateHandoff.artifactType === "Aggregate evidence bundle summary", "aggregate evidence bundle candidate fixture did not classify as bundle");
  assert(aggregateEvidenceBundleCandidateModel.aggregateHandoff.deepInspectionCandidate.candidateTotal === 4, "nested evidence bundle deep-inspection candidate total was not preserved");
  assert(aggregateEvidenceBundleCandidateModel.aggregateHandoff.deepInspectionCandidate.providerConfigured === true, "nested evidence bundle provider flag was not preserved");
  state.model = aggregateEvidenceBundleCandidateModel;
  renderAggregateHandoff();
  assert(els.aggregateHandoff.innerHTML.includes("深度检查候选摘要"), "aggregate evidence bundle did not render nested deep-inspection candidate summary");
  assert(els.aggregateHandoff.innerHTML.includes("quality_content_edge_cutoff_candidate"), "aggregate evidence bundle did not render nested deep-inspection candidate reason code");
  assertPublicSafe(els.aggregateHandoff.innerHTML, "nested aggregate evidence bundle deep-inspection candidate rendering");

  const finalHandoffPassModel = inferArtifact(finalHandoffPassFixture);
  assert(finalHandoffPassModel.sourceType === "aggregate-handoff", "passing final handoff fixture did not load as aggregate handoff");
  assert(finalHandoffPassModel.aggregateHandoff.artifactType === "Final production handoff summary", "passing final handoff fixture did not classify as final handoff");
  assert(finalHandoffPassModel.aggregateHandoff.status === "pass", "passing final handoff status was not pass");
  assert(finalHandoffPassModel.aggregateHandoff.readyFlag === true, "passing final handoff ready flag was not true");
  assert(finalHandoffPassModel.aggregateHandoff.checksPassed === 6, "passing final handoff checks passed were not preserved");
  assert(finalHandoffPassModel.aggregateHandoff.checksFailed === 0, "passing final handoff checks failed were not preserved");
  assert(finalHandoffPassModel.aggregateHandoff.blockingItemCount === 0, "passing final handoff blocking count was not zero");
  assert(finalHandoffPassModel.aggregateHandoff.counts.processingResumedFiles === 2, "passing final handoff processing resumed count was not preserved");
  assert(finalHandoffPassModel.aggregateHandoff.counts.processingDuplicateReusedFiles === 3, "passing final handoff duplicate reuse count was not preserved");
  assert(finalHandoffPassModel.aggregateHandoff.counts.processingExistingDerivativeReusedFiles === 4, "passing final handoff existing derivative reuse count was not preserved");
  assert(finalHandoffPassModel.aggregateHandoff.deepInspectionCandidate.candidateTotal === 3, "nested final handoff deep-inspection candidate total was not preserved");
  assert(finalHandoffPassModel.aggregateHandoff.reviewDecisionVerification.totalDecisions === 19, "nested final handoff review decision total was not preserved");
  assert(finalHandoffPassModel.aggregateHandoff.reviewDecisionVerification.acceptedCount === 11, "nested final handoff review decision accepted count was not preserved");
  assert(finalHandoffPassModel.aggregateHandoff.reviewDecisionVerification.rejectedCount === 5, "nested final handoff review decision rejected count was not preserved");
  assert(finalHandoffPassModel.aggregateHandoff.reviewDecisionVerification.reworkCount === 2, "nested final handoff review decision rework count was not preserved");
  assert(finalHandoffPassModel.aggregateHandoff.reviewDecisionVerification.pendingCount === 1, "nested final handoff review decision pending count was not preserved");
  assert(finalHandoffPassModel.aggregateHandoff.reviewDecisionVerification.warningCount === 1, "nested final handoff review decision warning count was not preserved");
  assert(countFor(finalHandoffPassModel.aggregateHandoff.reviewDecisionVerification.warningCodeCounts, "ignored_extra_decision_field") === 1, "nested final handoff review decision warning code count was not preserved");
  state.model = finalHandoffPassModel;
  renderAggregateHandoff();
  assert(els.aggregateHandoff.innerHTML.includes("最终生产交接摘要"), "passing final handoff did not render final handoff type");
  assert(els.aggregateHandoff.innerHTML.includes("交接就绪"), "passing final handoff did not render ready flag label");
  assert(els.aggregateHandoff.innerHTML.includes("检查通过数"), "passing final handoff did not render checks passed");
  assert(els.aggregateHandoff.innerHTML.includes("检查失败数"), "passing final handoff did not render checks failed");
  assert(els.aggregateHandoff.innerHTML.includes("处理已有衍生复用"), "passing final handoff did not render existing derivative reuse count");
  assert(els.aggregateHandoff.innerHTML.includes("工件存在与状态"), "passing final handoff did not render artifact status summary");
  assert(els.aggregateHandoff.innerHTML.includes("深度检查候选摘要"), "passing final handoff did not render nested deep-inspection candidate summary");
  assert(els.aggregateHandoff.innerHTML.includes("processing_review_status_failed"), "passing final handoff did not render nested deep-inspection candidate reason code");
  assert(els.aggregateHandoff.innerHTML.includes("复核决定验证摘要"), "passing final handoff did not render nested review decision verification summary");
  assert(els.aggregateHandoff.innerHTML.includes("决定总数"), "passing final handoff did not render nested review decision total");
  assert(els.aggregateHandoff.innerHTML.includes("ignored_extra_decision_field"), "passing final handoff did not render nested review decision warning code count");
  assert(els.aggregateHandoff.innerHTML.includes("隐私状态"), "passing final handoff did not render privacy status");
  assert(!els.aggregateHandoff.innerHTML.includes("PRIVATE-LOCAL-ID"), "passing final handoff rendered nested review decision local ID");
  assert(!els.aggregateHandoff.innerHTML.includes("source_type"), "passing final handoff rendered nested review decision source type");
  assert(!els.aggregateHandoff.innerHTML.includes("scan-qc-review-decisions.local.v1"), "passing final handoff rendered nested review decision source schema");
  assertPublicSafe(els.aggregateHandoff.innerHTML, "nested final handoff deep-inspection candidate rendering");

  const finalHandoffBlockedModel = inferArtifact(finalHandoffBlockedFixture);
  assert(finalHandoffBlockedModel.sourceType === "aggregate-handoff", "blocked final handoff fixture did not load as aggregate handoff");
  assert(finalHandoffBlockedModel.aggregateHandoff.artifactType === "Final production handoff summary", "blocked final handoff fixture did not classify as final handoff");
  assert(finalHandoffBlockedModel.aggregateHandoff.status === "fail", "blocked final handoff status was not fail");
  assert(finalHandoffBlockedModel.aggregateHandoff.readyFlag === false, "blocked final handoff ready flag was not false");
  assert(finalHandoffBlockedModel.aggregateHandoff.checksPassed === 4, "blocked final handoff checks passed were not preserved");
  assert(finalHandoffBlockedModel.aggregateHandoff.checksFailed === 2, "blocked final handoff checks failed were not preserved");
  assert(finalHandoffBlockedModel.aggregateHandoff.blockingItemCount === 2, "blocked final handoff blocking count was not preserved");
  assert(finalHandoffBlockedModel.aggregateHandoff.blockingCodes.includes("aggregate_handoff_acceptance_blocker"), "blocked final handoff blocker code was not preserved");
  assert(finalHandoffBlockedModel.artifactReadiness.ready === false, "blocked final handoff readiness was unexpectedly ready");
  state.model = finalHandoffBlockedModel;
  renderAggregateHandoff();
  assert(els.aggregateHandoff.innerHTML.includes("aggregate_handoff_acceptance_blocker"), "blocked final handoff did not render blocker code");
  assert(els.aggregateHandoff.innerHTML.includes("aggregate_handoff_artifact_blocker"), "blocked final handoff did not render second blocker code");
  assert(els.aggregateHandoff.innerHTML.includes("aggregate_warning_handoff_recheck"), "blocked final handoff did not render warning code");
  assert(els.aggregateHandoff.innerHTML.includes("公开交接未就绪"), "blocked final handoff did not render not-ready checklist summary");

  const validationIndexPassModel = inferArtifact(validationIndexPassFixture);
  assert(validationIndexPassModel.sourceType === "aggregate-handoff", "passing validation index fixture did not load as aggregate handoff");
  assert(validationIndexPassModel.aggregateHandoff.artifactType === "Public-safe validation index", "passing validation index fixture did not classify as validation index");
  assert(validationIndexPassModel.aggregateHandoff.status === "pass", "passing validation index status was not pass");
  assert(validationIndexPassModel.aggregateHandoff.validationIndex.artifactsPresent === 6, "passing validation index artifacts_present was not preserved");
  assert(validationIndexPassModel.aggregateHandoff.validationIndex.artifactsFailed === 0, "passing validation index artifacts_failed was not preserved");
  assert(validationIndexPassModel.aggregateHandoff.validationIndex.artifactsMissing === 0, "passing validation index artifacts_missing was not preserved");
  assert(validationIndexPassModel.aggregateHandoff.validationIndex.unknownInputs === 0, "passing validation index unknown_inputs was not preserved");
  assert(validationIndexPassModel.aggregateHandoff.validationIndex.checksPassed === 12, "passing validation index checks_passed was not preserved");
  assert(validationIndexPassModel.aggregateHandoff.validationIndex.checksFailed === 0, "passing validation index checks_failed was not preserved");
  assert(validationIndexPassModel.aggregateHandoff.validationIndex.blockingItemCount === 0, "passing validation index blocking count was not preserved");
  assert(validationIndexPassModel.aggregateHandoff.validationIndexReviewDecisionCoverage.length === 3, "passing validation index did not preserve review decision handoff coverage");
  assert(validationIndexPassModel.aggregateHandoff.validationIndexReviewDecisionCoverage[0].warningCodeCounts[0].name === "ignored_extra_decision_field", "passing validation index did not preserve direct review decision warning code counts");
  assert(validationIndexPassModel.aggregateHandoff.validationIndexReviewDecisionCoverage[1].checksPassed === 4, "passing validation index did not preserve aggregate evidence nested review decision checks");
  assert(validationIndexPassModel.aggregateHandoff.validationIndexReviewDecisionCoverage[2].checksPassed === 5, "passing validation index did not preserve final handoff nested review decision checks");
  state.model = validationIndexPassModel;
  renderAggregateHandoff();
  assert(els.aggregateHandoff.innerHTML.includes("公开安全验证索引"), "passing validation index did not render index section");
  assert(els.aggregateHandoff.innerHTML.includes("复核决定交接覆盖"), "passing validation index did not render review decision handoff coverage");
  assert(els.aggregateHandoff.innerHTML.includes("aggregate_evidence_bundle_summary.json nested review decision verification"), "passing validation index did not render aggregate evidence nested review decision coverage");
  assert(els.aggregateHandoff.innerHTML.includes("final_production_handoff_summary.json nested review decision verification"), "passing validation index did not render final handoff nested review decision coverage");
  assert(els.aggregateHandoff.innerHTML.includes("ignored_extra_decision_field"), "passing validation index did not render direct review decision warning code count");
  assert(els.aggregateHandoff.innerHTML.includes("工件存在数"), "passing validation index did not render artifacts_present");
  assert(els.aggregateHandoff.innerHTML.includes("隐私仅汇总状态"), "passing validation index did not render aggregate-only privacy status");
  assert(els.aggregateHandoff.innerHTML.includes("frontend_workbench_validation.json"), "passing validation index did not render known public-safe filename");
  assert(!els.aggregateHandoff.innerHTML.includes("PRIVATE-LOCAL-ID"), "passing validation index rendered nested review decision local ID");
  assert(!els.aggregateHandoff.innerHTML.includes("source_type"), "passing validation index rendered nested review decision source type");
  assert(!els.aggregateHandoff.innerHTML.includes("scan-qc-review-decisions.local.v1"), "passing validation index rendered nested review decision source schema");

  const validationIndexBlockedModel = inferArtifact(validationIndexBlockedFixture);
  assert(validationIndexBlockedModel.sourceType === "aggregate-handoff", "blocked validation index fixture did not load as aggregate handoff");
  assert(validationIndexBlockedModel.aggregateHandoff.artifactType === "Public-safe validation index", "blocked validation index fixture did not classify as validation index");
  assert(validationIndexBlockedModel.aggregateHandoff.status === "fail", "blocked validation index status was not fail");
  assert(validationIndexBlockedModel.aggregateHandoff.validationIndex.artifactsFailed === 1, "blocked validation index artifacts_failed was not preserved");
  assert(validationIndexBlockedModel.aggregateHandoff.validationIndex.artifactsMissing === 2, "blocked validation index artifacts_missing was not preserved");
  assert(validationIndexBlockedModel.aggregateHandoff.validationIndex.unknownInputs === 1, "blocked validation index unknown_inputs was not preserved");
  assert(validationIndexBlockedModel.aggregateHandoff.validationIndex.checksFailed === 4, "blocked validation index checks_failed was not preserved");
  assert(validationIndexBlockedModel.aggregateHandoff.validationIndex.blockingItemCount === 3, "blocked validation index blocking count was not preserved");
  assert(validationIndexBlockedModel.aggregateHandoff.blockingCodes.includes("artifact_status_failed"), "blocked validation index blocker code was not preserved");
  state.model = validationIndexBlockedModel;
  renderAggregateHandoff();
  assert(els.aggregateHandoff.innerHTML.includes("aggregate_artifact_missing"), "blocked validation index did not render missing artifact blocker code");
  assert(els.aggregateHandoff.innerHTML.includes("unknown_public_safe_artifact"), "blocked validation index did not render unknown input blocker code");
  assert(!els.aggregateHandoff.innerHTML.includes("source_value"), "blocked validation index rendered source values");

  const missingChecklistModel = inferArtifact(missingChecklistFixture);
  assert(missingChecklistModel.artifactReadiness.ready === false, "missing checklist fixture was unexpectedly ready");
  assert(missingChecklistModel.artifactReadiness.missingCount >= 1, "missing checklist fixture did not count missing artifacts");
  assert(missingChecklistModel.artifactReadiness.blockingCount === 2, "missing checklist fixture did not preserve blocking counts");
  assert(missingChecklistModel.artifactReadiness.warningCount === 3, "missing checklist fixture did not preserve warning counts");
  assert(missingChecklistModel.artifactReadiness.staleCount === 1, "missing checklist fixture did not count stale artifact");
  state.model = missingChecklistModel;
  renderAggregateHandoff();
  assert(els.aggregateHandoff.innerHTML.includes("公开交接未就绪"), "missing checklist did not render not-ready summary");
  assert(els.aggregateHandoff.innerHTML.includes("missing"), "missing checklist did not render missing status");
  assert(els.aggregateHandoff.innerHTML.includes("stale"), "missing checklist did not render stale status");

  const unsupportedSchemaModel = inferArtifact(unsupportedSchemaFixture);
  assert(unsupportedSchemaModel.aggregateHandoff.artifactType === "Public-safe artifact readiness checklist", "unsupported schema fixture did not classify by public-safe artifact type");
  assert(unsupportedSchemaModel.artifactCompatibility.schemaRecognized === false, "unsupported schema fixture was unexpectedly recognized");
  assert(unsupportedSchemaModel.artifactCompatibility.warningCount >= 1, "unsupported schema fixture did not produce warning count");
  state.model = unsupportedSchemaModel;
  renderAggregateHandoff();
  assert(els.aggregateHandoff.innerHTML.includes("unsupported_public_safe_schema_version"), "unsupported schema fixture did not render unsupported schema warning");
  assert(els.aggregateHandoff.innerHTML.includes("不支持或未知"), "unsupported schema fixture did not render unknown schema wording");

  const failingPrivacyModel = inferArtifact(failingPrivacyFixture);
  assert(failingPrivacyModel.artifactCompatibility.privacySummaryStatus === "fail", "failing privacy fixture did not produce privacy fail status");
  assert(failingPrivacyModel.artifactCompatibility.blockingCount >= 1, "failing privacy fixture did not produce blocking count");
  state.model = failingPrivacyModel;
  renderAggregateHandoff();
  assert(els.aggregateHandoff.innerHTML.includes("privacy_summary_fail"), "failing privacy fixture did not render privacy failure diagnostic");
  assert(els.aggregateHandoff.innerHTML.includes("诊断阻塞数"), "failing privacy fixture did not render diagnostic blocking count");

  const missingPrivacyModel = inferArtifact(missingPrivacyFixture);
  assert(missingPrivacyModel.artifactCompatibility.privacySummaryStatus === "missing", "missing privacy fixture did not produce privacy missing status");
  state.model = missingPrivacyModel;
  renderAggregateHandoff();
  assert(els.aggregateHandoff.innerHTML.includes("privacy_summary_missing"), "missing privacy fixture did not render privacy missing diagnostic");

  const providerProbeModel = inferArtifact(providerProbeFixture);
  assert(providerProbeModel.sourceType === "aggregate-handoff", "provider probe fixture did not load as aggregate handoff");
  assert(providerProbeModel.aggregateHandoff.artifactType === "Provider capability probe summary", "provider probe fixture did not classify as provider capability probe");
  assert(providerProbeModel.aggregateHandoff.status === "fail", "provider probe blocked status did not normalize to fail");
  assert(providerProbeModel.aggregateHandoff.providerProbe.providerCount === 3, "provider probe provider count was not preserved");
  assert(providerProbeModel.aggregateHandoff.providerProbe.configuredProviderCount === 0, "provider probe configured provider count was not preserved");
  assert(providerProbeModel.aggregateHandoff.providerProbe.providersConfigured === false, "provider probe configured flag was not preserved");
  assert(providerProbeModel.aggregateHandoff.providerProbe.visibleGpuCount === 0, "provider probe GPU count was not preserved");
  assert(providerProbeModel.aggregateHandoff.providerProbe.visibleModelCount === 0, "provider probe model count was not preserved");
  assert(providerProbeModel.aggregateHandoff.providerProbe.optionalPackageVisibleCount === 1, "provider probe optional package visible count was not preserved");
  assert(providerProbeModel.aggregateHandoff.providerProbe.optionalPackageMissingCount === 2, "provider probe optional package missing count was not preserved");
  assert(providerProbeModel.aggregateHandoff.providerProbe.privacyStatus === "public-safe", "provider probe privacy status was not preserved");
  state.model = providerProbeModel;
  renderAggregateHandoff();
  assert(els.aggregateHandoff.innerHTML.includes("提供方能力探测"), "provider probe did not render probe section");
  assert(els.aggregateHandoff.innerHTML.includes("已配置提供方数"), "provider probe did not render configured count");
  assert(els.aggregateHandoff.innerHTML.includes("可见 GPU 数"), "provider probe did not render GPU count");
  assert(els.aggregateHandoff.innerHTML.includes("可见模型数"), "provider probe did not render model count");
  assert(els.aggregateHandoff.innerHTML.includes("缺失可选包数"), "provider probe did not render optional package count");
  assert(els.aggregateHandoff.innerHTML.includes("探测隐私状态"), "provider probe did not render privacy status");
  assert(els.aggregateHandoff.innerHTML.includes("provider_probe_gpu_not_visible"), "provider probe did not render warning code");

  const processingReviewModel = inferArtifact(processingReviewFixture);
  assert(processingReviewModel.sourceType === "aggregate-handoff", "processing review fixture did not load as aggregate handoff");
  assert(processingReviewModel.aggregateHandoff.artifactType === "Processing-review package summary", "processing review fixture did not classify as package summary");
  assert(processingReviewModel.aggregateHandoff.processingReview.processedCount === 4, "processing review processed count was not preserved");
  assert(processingReviewModel.aggregateHandoff.processingReview.failedCount === 1, "processing review failed count was not preserved");
  assert(processingReviewModel.aggregateHandoff.processingReview.warningCount === 2, "processing review warning count was not preserved");
  assert(processingReviewModel.aggregateHandoff.processingReview.reviewTargetCount === 6, "processing review target count was not preserved");
  assert(processingReviewModel.aggregateHandoff.processingReview.resumedCount === 2, "processing review resumed count was not preserved");
  assert(processingReviewModel.aggregateHandoff.processingReview.duplicateReusedCount === 3, "processing review duplicate reuse count was not preserved");
  assert(processingReviewModel.aggregateHandoff.processingReview.existingDerivativeReusedCount === 4, "processing review existing derivative reuse count was not preserved");
  assert(processingReviewModel.aggregateHandoff.processingReview.localOnly === true, "processing review local-only status was not preserved");
  assert(processingReviewModel.reviewTargets.length === 6, "processing review synthetic targets were not created from summary count");
  assert(processingReviewModel.reviewTargets[0].scope === "processing_review", "processing review target scope changed");
  assert(processingReviewModel.reviewTargets[0].localId === "PR0001", "processing review target local ID changed");
  state.model = processingReviewModel;
  renderAggregateHandoff();
  assert(els.aggregateHandoff.innerHTML.includes("处理复核包摘要"), "processing review did not render package summary section");
  assert(els.aggregateHandoff.innerHTML.includes("已处理数"), "processing review did not render processed count");
  assert(els.aggregateHandoff.innerHTML.includes("失败数"), "processing review did not render failed count");
  assert(els.aggregateHandoff.innerHTML.includes("复核目标数"), "processing review did not render target count");
  assert(els.aggregateHandoff.innerHTML.includes("敏感性/仅本地状态"), "processing review did not render local-only sensitivity");
  assert(els.aggregateHandoff.innerHTML.includes("处理重复复用"), "processing review did not render duplicate reuse count");
  assert(els.aggregateHandoff.innerHTML.includes("处理已有衍生复用"), "processing review did not render existing derivative reuse count");
  assert(els.aggregateHandoff.innerHTML.includes("处理状态计数"), "processing review did not render status counts");
  renderReview();
  assert(els.reviewTargetList.innerHTML.includes("processing_review"), "processing review target list did not render scope");
  assert(els.reviewTargetList.innerHTML.includes("PR0001"), "processing review target list did not render first synthetic local ID");
  assert(els.reviewTargetList.innerHTML.includes("failed"), "processing review target list did not render status");
  assert(els.reviewTargetList.innerHTML.includes('data-review-scope="processing_review"'), "processing review target list did not render decision control scope");
  assert(els.reviewTargetList.innerHTML.includes('data-review-id="PR0001"'), "processing review target list did not render first decision control ID");
  assertPublicSafe(els.reviewTargetList.innerHTML, "processing review target list");
  state.decisions.set(decisionKey("processing_review", "PR0001"), "accepted_issue");
  const processingReviewExport = buildReviewSummary();
  assert(processingReviewExport.decisions.length === 6, "processing review export did not include synthetic review targets");
  assert(processingReviewExport.decisions[0].scope === "processing_review", "processing review export scope changed");
  assert(Object.keys(processingReviewExport.decisions[0]).sort().join(",") === "decision,local_id,scope", "processing review export included unexpected decision fields");
  assert(!JSON.stringify(processingReviewExport).includes("processed_files"), "processing review export leaked package summary fields");

  assert(Array.isArray(DEMO_FIXTURES), "demo fixture gallery is not an array");
  assert(DEMO_FIXTURES.length >= 5, "demo fixture gallery does not cover at least five options");
  const demoLabels = DEMO_FIXTURES.map(item => item.label);
  [
    "通过的工作台公开摘要",
    "阻塞的工作台公开摘要",
    "已识别的通过复核摘要",
    "通过的验收摘要",
    "完整的公开安全就绪清单",
    "通过的最终生产交接",
    "阻塞的最终生产交接",
    "通过的公开安全验证索引",
    "阻塞的公开安全验证索引",
    "已禁用的提供方能力探测",
    "合成处理复核包摘要",
    "不支持架构的兼容性警告",
    "隐私摘要失败诊断",
    "隐私摘要缺失诊断"
  ].forEach(label => assert(demoLabels.includes(label), "missing demo fixture label: " + label));

  DEMO_FIXTURES.forEach(item => {{
    assert(item && item.id && item.label && item.payload, "demo fixture is missing id, label, or payload");
    const serialized = JSON.stringify(item.payload);
    [
      "filename",
      "path",
      "hash",
      "sha256",
      "ocr_text",
      "thumbnail",
      "image_content",
      "image_bytes",
      "manifest",
      "reviewer_notes",
      "derivative_image",
      "preview_filename",
      "preview_object_url"
    ].forEach(field => assert(!serialized.includes(String.fromCharCode(34) + field + String.fromCharCode(34)), "demo fixture " + item.id + " includes forbidden field " + field));
    const model = inferArtifact(cloneDemoPayload(item.payload));
    assert(model.sourceType === "aggregate-handoff", "demo fixture " + item.id + " did not load through aggregate inference");
    state.model = model;
    renderAggregateHandoff();
    assert(els.aggregateHandoff.innerHTML.includes("公开安全工件兼容性诊断"), "demo fixture " + item.id + " did not render diagnostics");
    assert(els.aggregateHandoff.innerHTML.includes("仅汇总状态"), "demo fixture " + item.id + " did not render aggregate summary");
  }});

  loadDemoFixture("recognized-review-pass");
  assert(els.status.textContent.includes("已加载公开安全演示夹具：已识别的通过复核摘要。"), "demo load button path did not report selected fixture");
  assert(els.aggregateHandoff.innerHTML.includes("复核摘要"), "demo load path did not render review summary");
  loadDemoFixture("workbench-public-summary-pass");
  assert(els.aggregateHandoff.innerHTML.includes("工作台公开摘要"), "demo load path did not render passing workbench public summary");
  assert(els.aggregateHandoff.innerHTML.includes("acceptance_status: pass"), "demo load path did not render passing workbench workflow state");
  assert(state.model.aggregateHandoff.despeckleBackend.requestedBackend === "numpy", "passing workbench did not preserve requested despeckle backend");
  assert(state.model.aggregateHandoff.despeckleBackend.effectiveBackendMode === "numpy", "passing workbench did not preserve effective despeckle backend");
  assert(state.model.aggregateHandoff.despeckleBackend.numpyAvailable === true, "passing workbench did not preserve NumPy availability");
  assert(state.model.aggregateHandoff.despeckleBackend.backendCounts.some(item => item.name === "numpy" && item.count === 7), "passing workbench did not preserve NumPy backend count");
  assert(state.model.aggregateHandoff.despeckleBackend.fallbackCount === 0, "passing workbench did not preserve zero fallback count");
  assert(els.aggregateHandoff.innerHTML.includes("去噪点后端摘要"), "demo load path did not render despeckle backend section");
  assert(els.aggregateHandoff.innerHTML.includes("请求后端"), "demo load path did not render requested backend label");
  assert(els.aggregateHandoff.innerHTML.includes("有效后端模式"), "demo load path did not render effective backend label");
  assert(els.aggregateHandoff.innerHTML.includes("请求 NumPy 后回退数"), "demo load path did not render requested NumPy fallback label");
  loadDemoFixture("workbench-public-summary-blocked");
  assert(els.aggregateHandoff.innerHTML.includes("unsupported_input"), "demo load path did not render blocked workbench unsupported input aggregate code");
  assert(els.aggregateHandoff.innerHTML.includes("阻塞代码"), "demo load path did not render blocked workbench aggregate code counts");
  assert(state.model.aggregateHandoff.despeckleBackend.requestedBackend === "numpy", "blocked workbench did not preserve requested despeckle backend");
  assert(state.model.aggregateHandoff.despeckleBackend.effectiveBackendMode === "fallback", "blocked workbench did not preserve all-fallback effective backend");
  assert(state.model.aggregateHandoff.despeckleBackend.numpyAvailable === false, "blocked workbench did not preserve unavailable NumPy status");
  assert(state.model.aggregateHandoff.despeckleBackend.backendCounts.some(item => item.name === "fallback" && item.count === 7), "blocked workbench did not preserve fallback backend count");
  assert(state.model.aggregateHandoff.despeckleBackend.requestedNumpyFallbackCount === 7, "blocked workbench did not preserve requested NumPy fallback count");
  assert(els.aggregateHandoff.innerHTML.includes("despeckle_numpy_unavailable_fallback"), "demo load path did not render unavailable NumPy warning");
  assert(els.aggregateHandoff.innerHTML.includes("despeckle_numpy_requested_all_fallback"), "demo load path did not render all-fallback NumPy warning");
  loadDemoFixture("complete-readiness-checklist");
  assert(els.aggregateHandoff.innerHTML.includes("公开安全工件就绪清单"), "demo load path did not render readiness checklist");
  loadDemoFixture("final-handoff-pass");
  assert(els.aggregateHandoff.innerHTML.includes("最终生产交接摘要"), "demo load path did not render passing final handoff");
  assert(els.aggregateHandoff.innerHTML.includes("交接就绪"), "demo load path did not render passing final handoff ready flag");
  loadDemoFixture("final-handoff-blocked");
  assert(els.aggregateHandoff.innerHTML.includes("aggregate_handoff_acceptance_blocker"), "demo load path did not render blocked final handoff");
  assert(els.aggregateHandoff.innerHTML.includes("公开交接未就绪"), "demo load path did not render blocked final handoff readiness");
  loadDemoFixture("validation-index-pass");
  assert(els.aggregateHandoff.innerHTML.includes("公开安全验证索引"), "demo load path did not render passing validation index type");
  assert(els.aggregateHandoff.innerHTML.includes("工件存在数"), "demo load path did not render passing validation index summary");
  loadDemoFixture("validation-index-blocked");
  assert(els.aggregateHandoff.innerHTML.includes("unknown_public_safe_artifact"), "demo load path did not render blocked validation index blocker");
  assert(els.aggregateHandoff.innerHTML.includes("工件缺失数"), "demo load path did not render blocked validation index missing count");
  loadDemoFixture("provider-capability-probe-disabled");
  assert(els.aggregateHandoff.innerHTML.includes("提供方能力探测摘要"), "demo load path did not render provider probe type");
  assert(els.aggregateHandoff.innerHTML.includes("提供方能力探测"), "demo load path did not render provider probe section");
  assert(els.aggregateHandoff.innerHTML.includes("provider_probe_optional_packages_not_installed"), "demo load path did not render provider probe warning");
  loadDemoFixture("processing-review-package-summary");
  assert(els.aggregateHandoff.innerHTML.includes("处理复核包摘要"), "demo load path did not render processing review type");
  assert(els.aggregateHandoff.innerHTML.includes("处理复核包摘要"), "demo load path did not render processing review section");
  assert(reviewTargets().length === 6, "demo load path did not expose synthetic processing review targets");
  loadDemoFixture("unsupported-schema-warning");
  assert(els.aggregateHandoff.innerHTML.includes("unsupported_public_safe_schema_version"), "demo load path did not render unsupported schema diagnostic");
  loadDemoFixture("privacy-diagnostic-fail");
  assert(els.aggregateHandoff.innerHTML.includes("privacy_summary_fail"), "demo load path did not render failing privacy diagnostic");
  loadDemoFixture("privacy-diagnostic-missing");
  assert(els.aggregateHandoff.innerHTML.includes("privacy_summary_missing"), "demo load path did not render missing privacy diagnostic");
`, context);
"""

    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as handle:
        handle.write(runner)
        runner_path = Path(handle.name)

    try:
        completed = subprocess.run(
            ["node", str(runner_path)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError:
        return ["Node.js is required for executable aggregate fixture checks but was not found on PATH"]
    finally:
        runner_path.unlink(missing_ok=True)

    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"node exited {completed.returncode}"
        return [f"executable aggregate fixture check failed: {detail}"]
    return []


def validate_executable_preview_lifecycle(html: str) -> list[str]:
    script_match = re.search(r"<script>(?P<script>.*?)</script>", html, re.DOTALL)
    if not script_match:
        return ["missing executable workbench script"]

    runner = f"""
const vm = require("node:vm");
const workbenchScript = {script_match.group("script")!r};
const elements = new Map();
const createdUrls = [];
const revokedUrls = [];
const eventHandlers = {{}};

function element(id) {{
  if (!elements.has(id)) {{
    elements.set(id, {{
      id,
      value: "",
      innerHTML: "",
      textContent: "",
      className: "",
      disabled: false,
      files: [],
      dataset: {{}},
      classList: {{
        add() {{}},
        remove() {{}}
      }},
      addEventListener(type, handler) {{
        eventHandlers[id + ":" + type] = handler;
      }},
      querySelectorAll() {{
        return [];
      }},
      click() {{}}
    }});
  }}
  return elements.get(id);
}}

const context = {{
  assert,
  assertPublicSafe,
  console,
  createdUrls,
  revokedUrls,
  eventHandlers,
  Blob: function Blob() {{}},
  Date,
  Map,
  Number,
  Set,
  String,
  JSON,
  Array,
  URL: {{
    createObjectURL(file) {{
      const url = "blob:synthetic-preview-" + file.name + "-" + createdUrls.length;
      createdUrls.push(url);
      return url;
    }},
    revokeObjectURL(url) {{
      revokedUrls.push(url);
    }}
  }},
  document: {{
    getElementById: element,
    createElement: element
  }},
  window: {{
    addEventListener(type, handler) {{
      eventHandlers["window:" + type] = handler;
    }}
  }}
}};

function assert(condition, message) {{
  if (!condition) throw new Error(message);
}}

function assertPublicSafe(value, label) {{
  const text = typeof value === "string" ? value : JSON.stringify(value);
  [
    "blob:synthetic-preview",
    "private_scan",
    "/Users/",
    "OCR_SECRET",
    "manifest_row",
    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  ].forEach(token => assert(!text.includes(token), label + " leaked " + token));
}}

vm.createContext(context);
vm.runInContext(workbenchScript + `
  function assertPreviewStatusSafe(slot, expectedStatus) {{
    const statusHtml = els.previewSlots[slot].privacyCopy.innerHTML;
    assert(statusHtml.includes(previewSlotLabel(slot) + "预览状态：" + expectedStatus), slot + " preview status did not render aggregate selected state");
    assert(statusHtml.includes(previewSlotLabel(slot) + "预览本地状态不包含在复核决定导出 JSON 中。"), slot + " preview export exclusion copy changed");
    assertPublicSafe(statusHtml, slot + " preview status");
    if (state.previews[slot].fileName) {{
      assert(!statusHtml.includes(state.previews[slot].fileName), slot + " preview status rendered local filename");
    }}
  }}

  const originalFirstFile = {{ name: "private_scan_original_alpha.tif", type: "image/tiff" }};
  const originalSecondFile = {{ name: "private_scan_original_beta.png", type: "image/png" }};
  const processedFirstFile = {{ name: "private_scan_processed_alpha.webp", type: "image/webp" }};
  const processedSecondFile = {{ name: "private_scan_processed_beta.jpg", type: "image/jpeg" }};

  loadPreviewFile("original", originalFirstFile);
  assert(state.previews.original.fileName === originalFirstFile.name, "first original preview filename was not tracked locally");
  assert(state.previews.original.objectUrl === "blob:synthetic-preview-private_scan_original_alpha.tif-0", "first original object URL was not created");
  assert(state.previews.processed.objectUrl === "", "original preview load should not populate processed preview");
  assert(createdUrls.length === 1, "first original preview did not call createObjectURL once");
  assert(revokedUrls.length === 0, "first original preview unexpectedly revoked a URL");
  assert(els.previewSlots.original.preview.innerHTML.includes("blob:synthetic-preview-private_scan_original_alpha.tif-0"), "original preview image did not render object URL locally");
  assertPreviewStatusSafe("original", "已选择");
  assertPreviewStatusSafe("processed", "未选择");

  loadPreviewFile("processed", processedFirstFile);
  assert(state.previews.processed.fileName === processedFirstFile.name, "first processed preview filename was not tracked locally");
  assert(state.previews.processed.objectUrl === "blob:synthetic-preview-private_scan_processed_alpha.webp-1", "first processed object URL was not created");
  assert(state.previews.original.objectUrl === "blob:synthetic-preview-private_scan_original_alpha.tif-0", "processed load should not replace original preview");
  assert(createdUrls.length === 2, "first processed preview did not call createObjectURL once");
  assert(revokedUrls.length === 0, "first processed preview unexpectedly revoked a URL");
  assert(els.previewSlots.processed.preview.innerHTML.includes("blob:synthetic-preview-private_scan_processed_alpha.webp-1"), "processed preview image did not render object URL locally");
  assertPreviewStatusSafe("original", "已选择");
  assertPreviewStatusSafe("processed", "已选择");

  assert(state.previewDisplay.mode === "fit", "preview display should default to fit mode");
  assert(els.previewZoomStatus.textContent === "预览显示：适应面板", "fit preview status did not render");
  setPreviewZoom("1.5");
  assert(state.previewDisplay.mode === "zoom", "zoom selection did not switch preview display mode");
  assert(state.previewDisplay.zoom === 1.5, "zoom selection did not store the selected zoom level locally");
  assert(els.previewZoomSelect.value === "1.5", "zoom selector did not reflect selected zoom level");
  assert(els.previewZoomStatus.textContent === "预览显示：150% 缩放", "zoom preview status did not render selected percentage");
  assert(els.previewSlots.original.preview.className.includes("preview-zoom"), "original preview did not receive shared zoom class");
  assert(els.previewSlots.processed.preview.className.includes("preview-zoom"), "processed preview did not receive shared zoom class");
  assert(els.previewSlots.original.preview.innerHTML.includes("--preview-zoom: 1.5"), "original preview did not receive shared zoom style");
  assert(els.previewSlots.processed.preview.innerHTML.includes("--preview-zoom: 1.5"), "processed preview did not receive shared zoom style");
  setPreviewFitMode();
  assert(state.previewDisplay.mode === "fit", "fit control did not restore fit mode");
  assert(!els.previewSlots.original.preview.className.includes("preview-zoom"), "fit mode left original preview zoom class active");
  assert(!els.previewSlots.processed.preview.className.includes("preview-zoom"), "fit mode left processed preview zoom class active");
  setPreviewZoom("2");
  resetPreviewZoom();
  assert(state.previewDisplay.mode === "fit", "reset zoom did not return to fit mode");
  assert(state.previewDisplay.zoom === 1, "reset zoom did not restore 100 percent zoom state");
  assert(els.previewZoomStatus.textContent === "预览显示：适应面板", "reset zoom did not restore fit status");

  state.model = {{
    sourceType: "scan-report",
    metrics: {{ totalBatches: 0, totalFindings: 0, p0: 0, p1: 0, p2: 0 }},
    batches: [],
    findings: []
  }};
  const exportWhilePreviewLoaded = JSON.stringify(buildReviewSummary());
  assertPublicSafe(exportWhilePreviewLoaded, "review export");
  assert(!exportWhilePreviewLoaded.includes(originalFirstFile.name), "review export leaked original preview filename");
  assert(!exportWhilePreviewLoaded.includes(processedFirstFile.name), "review export leaked processed preview filename");
  assert(!exportWhilePreviewLoaded.includes("previewDisplay"), "review export leaked preview display state object");
  assert(!exportWhilePreviewLoaded.includes("preview_display_mode"), "review export leaked preview display mode field");
  assert(!exportWhilePreviewLoaded.includes("preview_zoom_level"), "review export leaked preview zoom level field");

  state.model = inferArtifact(cloneDemoPayload(DEMO_FIXTURES.find(item => item.id === "recognized-review-pass").payload));
  renderAggregateHandoff();
  assertPublicSafe(els.aggregateHandoff.innerHTML, "aggregate handoff");

  loadDemoFixture("complete-readiness-checklist");
  assertPublicSafe(JSON.stringify(DEMO_FIXTURES), "demo fixtures");
  assertPublicSafe(els.aggregateHandoff.innerHTML, "demo fixture render");
  assertPublicSafe(els.status.textContent, "demo fixture status");

  loadPreviewFile("original", originalSecondFile);
  assert(state.previews.original.fileName === originalSecondFile.name, "replacement original preview filename was not tracked locally");
  assert(state.previews.original.objectUrl === "blob:synthetic-preview-private_scan_original_beta.png-2", "replacement original object URL was not created");
  assert(state.previews.processed.objectUrl === "blob:synthetic-preview-private_scan_processed_alpha.webp-1", "original replacement should not replace processed preview");
  assert(createdUrls.length === 3, "replacement original preview did not call createObjectURL");
  assert(revokedUrls.includes("blob:synthetic-preview-private_scan_original_alpha.tif-0"), "original replacement did not revoke first original object URL");
  assertPreviewStatusSafe("original", "已选择");

  loadPreviewFile("processed", processedSecondFile);
  assert(state.previews.processed.fileName === processedSecondFile.name, "replacement processed preview filename was not tracked locally");
  assert(state.previews.processed.objectUrl === "blob:synthetic-preview-private_scan_processed_beta.jpg-3", "replacement processed object URL was not created");
  assert(createdUrls.length === 4, "replacement processed preview did not call createObjectURL");
  assert(revokedUrls.includes("blob:synthetic-preview-private_scan_processed_alpha.webp-1"), "processed replacement did not revoke first processed object URL");
  assertPreviewStatusSafe("processed", "已选择");

  clearPreviewState("original");
  assert(state.previews.original.fileName === "", "clear did not reset original preview filename");
  assert(state.previews.original.objectUrl === "", "clear did not reset original preview object URL");
  assert(state.previews.processed.objectUrl === "blob:synthetic-preview-private_scan_processed_beta.jpg-3", "clearing original should not clear processed preview");
  assert(els.previewSlots.original.file.value === "", "clear did not reset original preview file input");
  assert(revokedUrls.includes("blob:synthetic-preview-private_scan_original_beta.png-2"), "clear did not revoke replacement original object URL");
  assert(!els.previewSlots.original.preview.innerHTML.includes("blob:synthetic-preview"), "clear left object URL in original preview markup");
  assertPreviewStatusSafe("original", "未选择");

  clearPreviewState("processed");
  assert(state.previews.processed.fileName === "", "clear did not reset processed preview filename");
  assert(state.previews.processed.objectUrl === "", "clear did not reset processed preview object URL");
  assert(els.previewSlots.processed.file.value === "", "clear did not reset processed preview file input");
  assert(revokedUrls.includes("blob:synthetic-preview-private_scan_processed_beta.jpg-3"), "clear did not revoke replacement processed object URL");
  assert(!els.previewSlots.processed.preview.innerHTML.includes("blob:synthetic-preview"), "clear left object URL in processed preview markup");
  assertPreviewStatusSafe("processed", "未选择");

  loadPreviewFile("original", originalFirstFile);
  loadPreviewFile("processed", processedFirstFile);
  assert(typeof eventHandlers["window:beforeunload"] === "function", "beforeunload revocation handler was not registered");
  eventHandlers["window:beforeunload"]();
  assert(revokedUrls.includes("blob:synthetic-preview-private_scan_original_alpha.tif-4"), "beforeunload did not revoke active original object URL");
  assert(revokedUrls.includes("blob:synthetic-preview-private_scan_processed_alpha.webp-5"), "beforeunload did not revoke active processed object URL");
`, context);
"""

    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as handle:
        handle.write(runner)
        runner_path = Path(handle.name)

    try:
        completed = subprocess.run(
            ["node", str(runner_path)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError:
        return ["Node.js is required for executable preview lifecycle checks but was not found on PATH"]
    finally:
        runner_path.unlink(missing_ok=True)

    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"node exited {completed.returncode}"
        return [f"executable preview lifecycle check failed: {detail}"]
    return []


def validate_executable_review_decision_import_export(html: str) -> list[str]:
    script_match = re.search(r"<script>(?P<script>.*?)</script>", html, re.DOTALL)
    if not script_match:
        return ["missing executable workbench script"]

    runner = f"""
const vm = require("node:vm");
const workbenchScript = {script_match.group("script")!r};
const elements = new Map();
const eventHandlers = {{}};

function element(id) {{
  if (!elements.has(id)) {{
    elements.set(id, {{
      id,
      value: "",
      innerHTML: "",
      textContent: "",
      className: "",
      disabled: false,
      files: [],
      dataset: {{}},
      classList: {{
        add(cls) {{ this.ownerClass = cls; }},
        remove() {{}}
      }},
      addEventListener(type, handler) {{
        eventHandlers[id + ":" + type] = handler;
      }},
      querySelectorAll() {{
        return [];
      }},
      click() {{}}
    }});
  }}
  return elements.get(id);
}}

const context = {{
  assert,
  assertPublicSafe,
  console,
  Blob: function Blob() {{}},
  Date,
  Map,
  Number,
  Object,
  Set,
  String,
  JSON,
  Array,
  URL: {{
    createObjectURL() {{ return "blob:synthetic-download"; }},
    revokeObjectURL() {{}}
  }},
  document: {{
    getElementById: element,
    createElement: element
  }},
  window: {{
    addEventListener(type, handler) {{
      eventHandlers["window:" + type] = handler;
    }}
  }}
}};

function assert(condition, message) {{
  if (!condition) throw new Error(message);
}}

function assertPublicSafe(value, label) {{
  const text = typeof value === "string" ? value : JSON.stringify(value);
  [
    "/Users/",
    "C:\\\\",
    "PRIVATE_OCR_TEXT",
    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "private_scan_alpha.tif",
    "blob:synthetic-preview"
  ].forEach(token => assert(!text.includes(token), label + " leaked " + token));
}}

vm.createContext(context);
vm.runInContext(workbenchScript + `
  const scanReport = {{
    schema_version: "scan-qc-report.v1",
    generated_at: "2026-05-11T00:00:00Z",
    project: {{ project_id: "synthetic-public-project" }},
    manifest: {{ batch_id: "synthetic-batch" }},
    summary: {{
      total_files: 3,
      openable_files: 3,
      total_findings: 2,
      p0_findings: 0,
      p1_findings: 1,
      p2_findings: 1
    }},
    findings: [
      {{ rule: "skew_detected", severity: "P1", source: "rules", confidence: "medium", message: "Synthetic skew prompt" }},
      {{ rule: "blur_detected", severity: "P2", source: "rules", confidence: "low", message: "Synthetic blur prompt" }}
    ]
  }};

  state.model = inferArtifact(scanReport);
  state.selectedBatchId = state.model.batches[0].id;
  render();

  assert(reviewTargets().length === 3, "synthetic scan report did not create expected review targets");
  assert(els.decisionSummary.innerHTML.includes("目标总数"), "review completion summary did not render total targets");
  assert(els.decisionSummary.innerHTML.includes("待处理目标"), "review completion summary did not render pending targets");
  assert(els.decisionSummary.innerHTML.includes("未完成"), "initial review completion summary did not render not-complete status");
  assert(els.reviewCompletionGate.textContent.includes("复核完成门禁警告：未完成。"), "initial review completion gate warning did not render");
  assert(els.reviewCompletionGate.textContent.includes("目标总数：3。"), "initial completion gate did not render total target count");
  assert(els.reviewCompletionGate.textContent.includes("已复核目标：0。"), "initial completion gate did not render reviewed target count");
  assert(els.reviewCompletionGate.textContent.includes("待处理目标：3。"), "initial completion gate did not render pending target count");
  assert(els.reviewCompletionGate.textContent.includes("待处理: 3"), "initial completion gate did not render pending decision count");
  assert(els.reviewCompletionGate.textContent.includes("导出仅供参考且仍可使用。"), "completion gate warning changed export behavior wording");
  assertPublicSafe(els.reviewCompletionGate.textContent, "initial review completion gate");
  state.decisions.set(decisionKey("batch", "B0001"), "accepted_issue");
  state.decisions.set(decisionKey("finding", "F0001"), "false_positive");
  state.decisions.set(decisionKey("finding", "F0002"), "needs_rescan");
  renderReview();
  assert(els.decisionSummary.innerHTML.includes("已复核目标"), "review completion summary did not render reviewed targets");
  assert(els.decisionSummary.innerHTML.includes("已完成"), "review completion summary did not update to complete");
  assert(els.reviewCompletionGate.textContent.includes("复核完成门禁：已完成。"), "review completion gate did not render complete state");
  assert(els.reviewCompletionGate.textContent.includes("已复核目标：3。"), "complete gate did not render reviewed target count");
  assert(els.reviewCompletionGate.textContent.includes("待处理目标：0。"), "complete gate did not render zero pending target count");
  assert(els.reviewCompletionGate.textContent.includes("确认问题: 1"), "complete gate did not render accepted count");
  assert(els.reviewCompletionGate.textContent.includes("误报: 1"), "complete gate did not render false-positive count");
  assert(els.reviewCompletionGate.textContent.includes("需要重扫: 1"), "complete gate did not render needs-rescan count");
  assertPublicSafe(els.reviewCompletionGate.textContent, "complete review completion gate");
  const exported = buildReviewSummary();
  assert(exported.schema === "scan-qc-review-decisions.local.v1", "review export schema changed");
  assert(exported.source_type === "scan-report", "review export source type was not preserved");
  assert(exported.source_target_count === 3, "review export target count was not preserved");
  assert(exported.reviewed_targets === 3, "review export reviewed target count was not preserved");
  assert(exported.aggregate_counts.review_completion.total === 3, "review export completion total was not preserved");
  assert(exported.aggregate_counts.review_completion.reviewed === 3, "review export completion reviewed count was not preserved");
  assert(exported.aggregate_counts.review_completion.pending === 0, "review export completion pending count was not preserved");
  assert(exported.aggregate_counts.review_completion.complete === true, "review export completion status was not preserved");
  assert(exported.review_counts.accepted_issue === 1, "review export accepted count was not preserved");
  assert(exported.review_counts.false_positive === 1, "review export false-positive count was not preserved");
  assert(exported.review_counts.needs_rescan === 1, "review export needs-rescan count was not preserved");
  assert(exported.decisions.every(item => Object.keys(item).sort().join(",") === "decision,local_id,scope"), "review export included unexpected decision fields");
  assertPublicSafe(exported, "review export summary");

  resetReviewState();
  renderReview();
  assert(getDecision("batch", "B0001") === "pending", "reset did not clear batch decision");
  applyReviewDecisionSummary(exported);
  assert(state.importStatus.imported === 3, "valid review summary did not import all decisions");
  assert(state.importStatus.skipped === 0, "valid review summary skipped entries");
  assert(getDecision("batch", "B0001") === "accepted_issue", "batch decision was not restored");
  assert(getDecision("finding", "F0001") === "false_positive", "finding decision was not restored");
  assert(getDecision("finding", "F0002") === "needs_rescan", "second finding decision was not restored");
  assert(buildReviewSummary().aggregate_counts.review_completion.complete === true, "imported decisions did not restore completion status");
  assert(els.reviewImportStatus.textContent.includes("已导入 3 条复核决定；已跳过 0 条。"), "valid import status did not render aggregate counts");
  assertPublicSafe(els.reviewImportStatus.textContent, "valid import status");

  const invalidPayload = JSON.parse(JSON.stringify(exported));
  invalidPayload.decisions = [
    {{ scope: "batch", local_id: "B0001", decision: "fixed_externally" }},
    {{ scope: "finding", local_id: "F0001", decision: "false_positive", ocr_text: "PRIVATE_OCR_TEXT" }},
    {{ scope: "finding", local_id: "F0002", decision: "needs_rescan", hash: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" }},
    {{ scope: "finding", local_id: "F9999", decision: "accepted_issue" }},
    {{ scope: "finding", local_id: "F0002", decision: "not_a_status" }},
    {{ scope: "finding", local_id: "F0002", decision: "accepted_issue", reviewer_notes: "synthetic note" }},
    {{ scope: "finding", local_id: "F0002", decision: "accepted_issue", preview_display_mode: "zoom", preview_zoom_level: 2 }},
    {{ scope: "finding", local_id: "F0002", decision: "accepted_issue", extra_public_field: "ignored" }},
    "bad-entry"
  ];
  applyReviewDecisionSummary(invalidPayload);
  assert(state.importStatus.imported === 3, "invalid/private-bearing summary imported wrong count");
  assert(state.importStatus.skipped === 6, "invalid/private-bearing summary skipped wrong count");
  assert(getDecision("batch", "B0001") === "fixed_externally", "valid entry in conflict import was not applied");
  assert(getDecision("finding", "F0001") === "false_positive", "valid entry with ignored private field was not applied");
  assert(getDecision("finding", "F0002") === "needs_rescan", "first valid duplicate target decision was not preserved");
  assert(buildReviewSummary().aggregate_counts.review_completion.complete === true, "conflict import did not preserve completion recalculation");
  [
    "duplicate_decision=3",
    "ignored_private_field=4",
    "ignored_extra_field=1",
    "unknown_review_target=1",
    "unsupported_decision_status=1",
    "invalid_decision_entry=1"
  ].forEach(code => assert(state.importStatus.validationCodes.includes(code), "missing aggregate validation code " + code));
  assert(els.reviewImportStatus.textContent.includes("验证代码："), "invalid import status did not render validation codes");
  assertPublicSafe(els.reviewImportStatus.textContent, "invalid import status");
  assertPublicSafe(state.exportSummary, "post-import export summary");

  const runPlan = {{
    schema_version: "scan-qc-run-plan-summary.v1",
    generated_at: "2026-05-11T00:00:00Z",
    summary: {{ total_batches: 2, total_files: 10, total_findings: 0, processing_resumed_files: 2, processing_duplicate_reused_files: 3, processing_existing_derivative_reused_files: 4 }},
    batches: [
      {{ batch_id: "synthetic-batch-a", total_files: 5, openable_files: 5, total_findings: 0, p0_findings: 0, p1_findings: 0, p2_findings: 0 }},
      {{ batch_id: "synthetic-batch-b", total_files: 5, openable_files: 5, total_findings: 0, p0_findings: 0, p1_findings: 0, p2_findings: 0 }}
    ]
  }};
  state.model = inferArtifact(runPlan);
  assert(state.model.metrics.processingResumed === 2, "run-plan processing resumed count was not preserved");
  assert(state.model.metrics.processingDuplicateReused === 3, "run-plan duplicate reuse count was not preserved");
  assert(state.model.metrics.processingExistingDerivativeReused === 4, "run-plan existing derivative reuse count was not preserved");
  resetReviewState();
  render();
  assert(els.metrics.innerHTML.includes("处理已恢复"), "run-plan metrics did not render processing resumed count");
  assert(els.metrics.innerHTML.includes("处理重复复用"), "run-plan metrics did not render duplicate reuse count");
  assert(els.metrics.innerHTML.includes("处理已有衍生复用"), "run-plan metrics did not render existing derivative reuse count");
  assert(reviewTargets().length === 2, "run-plan synthetic targets were not exposed");
  state.decisions.set(decisionKey("batch", "B0001"), "fixed_externally");
  renderReview();
  let runPlanSummary = buildReviewSummary().aggregate_counts.review_completion;
  assert(runPlanSummary.total === 2, "run-plan completion total changed");
  assert(runPlanSummary.reviewed === 1, "run-plan reviewed target count did not update");
  assert(runPlanSummary.pending === 1, "run-plan pending target count did not update");
  assert(runPlanSummary.complete === false, "run-plan completion finished too early");
  assert(els.reviewCompletionGate.textContent.includes("复核完成门禁警告：未完成。"), "run-plan incomplete gate warning did not render");
  assert(els.reviewCompletionGate.textContent.includes("目标总数：2。"), "run-plan gate did not render total targets");
  assert(els.reviewCompletionGate.textContent.includes("已复核目标：1。"), "run-plan gate did not render reviewed targets");
  assert(els.reviewCompletionGate.textContent.includes("待处理目标：1。"), "run-plan gate did not render pending targets");
  state.decisions.set(decisionKey("batch", "B0002"), "blocked");
  renderReview();
  runPlanSummary = buildReviewSummary().aggregate_counts.review_completion;
  assert(runPlanSummary.complete === true, "run-plan completion did not finish after all targets changed");
  assert(els.reviewCompletionGate.textContent.includes("复核完成门禁：已完成。"), "run-plan complete gate did not render");
  assert(buildReviewSummary().review_counts.fixed_externally === 1, "run-plan fixed count was not preserved");
  assert(buildReviewSummary().review_counts.blocked === 1, "run-plan blocked count was not preserved");

  const processingReview = cloneDemoPayload(DEMO_FIXTURES.find(item => item.id === "processing-review-package-summary").payload);
  state.model = inferArtifact(processingReview);
  resetReviewState();
  render();
  assert(reviewTargets().length === 6, "processing-review synthetic targets were not exposed");
  assert(els.reviewFilterCount.textContent === "显示 6 / 6 个目标", "initial filtered count did not show visible and total targets");
  assert(els.reviewTargetList.innerHTML.includes("PR0001"), "processing-review target list did not render first target");
  assert(els.reviewTargetList.innerHTML.includes("failed"), "processing-review target list did not render synthetic target status");
  assert(els.reviewTargetList.innerHTML.includes("P1"), "processing-review target list did not render synthetic target severity");
  setReviewFilter("status", "failed");
  assert(els.reviewStatusFilter.value === "failed", "status filter control did not sync");
  assert(els.reviewFilterCount.textContent === "显示 1 / 6 个目标", "status filter did not update visible/total count");
  assert(els.reviewTargetList.innerHTML.includes("PR0001"), "status filter hid matching failed target");
  assert(!els.reviewTargetList.innerHTML.includes("PR0002"), "status filter left non-matching target visible");
  assert(buildReviewSummary().aggregate_counts.review_completion.total === 6, "filter changed export completion total");
  const bulkFailedResult = applyBulkVisibleReviewDecision("needs_rescan");
  assert(bulkFailedResult.visible === 1, "bulk status-filter action did not report the visible target count");
  assert(bulkFailedResult.updated === 1, "bulk status-filter action did not report the updated target count");
  assert(getDecision("processing_review", "PR0001") === "needs_rescan", "bulk action did not update visible failed target");
  assert(getDecision("processing_review", "PR0002") === "pending", "bulk action changed hidden target");
  assert(els.bulkReviewStatus.textContent.includes("可见目标：1。已更新目标：1。"), "bulk action status did not render aggregate counts only");
  assertPublicSafe(els.bulkReviewStatus.textContent, "bulk action status");
  assert(buildReviewSummary().aggregate_counts.review_completion.reviewed === 1, "bulk action did not update reviewed completion count");
  assert(buildReviewSummary().aggregate_counts.review_completion.pending === 5, "bulk action did not update pending completion count");
  assert(buildReviewSummary().review_counts.needs_rescan === 1, "bulk action did not update decision counts");
  assert(buildReviewSummary().decisions.length === 6, "bulk action removed hidden targets from export");
  assertPublicSafe(JSON.stringify(buildReviewSummary()), "bulk action review export summary");
  setReviewFilter("status", "all");
  setReviewFilter("severity", "P2");
  assert(els.reviewFilterCount.textContent === "显示 3 / 6 个目标", "severity filter did not update visible/total count");
  const bulkP2Result = applyBulkVisibleReviewDecision("blocked");
  assert(bulkP2Result.visible === 3, "bulk severity-filter action did not report visible target count");
  assert(bulkP2Result.updated === 3, "bulk severity-filter action did not report updated target count");
  assert(getDecision("processing_review", "PR0001") === "needs_rescan", "bulk severity action changed hidden failed target");
  assert(buildReviewSummary().review_counts.blocked === 3, "bulk severity action did not update blocked count");
  assert(buildReviewSummary().aggregate_counts.review_completion.reviewed === 4, "bulk severity action did not update aggregate reviewed count");
  setReviewFilter("scope", "batch");
  assert(els.reviewFilterCount.textContent === "显示 0 / 6 个目标", "combined filters did not update visible/total count");
  assert(els.reviewTargetList.innerHTML.includes("没有复核目标匹配当前筛选条件。"), "empty filtered state did not render");
  const emptyBulkResult = applyBulkVisibleReviewDecision("accepted_issue");
  assert(emptyBulkResult.visible === 0, "empty filtered bulk action reported visible targets");
  assert(emptyBulkResult.updated === 0, "empty filtered bulk action reported updated targets");
  assert(buildReviewSummary().aggregate_counts.review_completion.reviewed === 4, "empty filtered bulk action changed completion count");
  resetReviewState();
  renderReview();
  state.decisions.set(decisionKey("processing_review", "PR0001"), "accepted_issue");
  state.decisions.set(decisionKey("processing_review", "PR0002"), "false_positive");
  renderReview();
  setReviewFilter("decision", "accepted_issue");
  assert(els.reviewFilterCount.textContent === "显示 1 / 6 个目标", "decision filter did not update visible/total count");
  assert(els.reviewTargetList.innerHTML.includes("PR0001"), "decision filter hid matching accepted target");
  assert(!els.reviewTargetList.innerHTML.includes("PR0002"), "decision filter left non-matching false-positive target visible");
  const processingSummary = buildReviewSummary().aggregate_counts.review_completion;
  assert(processingSummary.total === 6, "processing-review completion total changed");
  assert(processingSummary.reviewed === 2, "processing-review reviewed target count did not update");
  assert(processingSummary.pending === 4, "processing-review pending target count did not update");
  assert(processingSummary.complete === false, "processing-review completion finished too early");
  assert(els.reviewCompletionGate.textContent.includes("复核完成门禁警告：未完成。"), "processing-review incomplete gate warning did not render");
  assert(els.reviewCompletionGate.textContent.includes("目标总数：6。"), "processing-review gate did not render total targets");
  assert(els.reviewCompletionGate.textContent.includes("已复核目标：2。"), "processing-review gate did not render reviewed targets");
  assert(els.reviewCompletionGate.textContent.includes("待处理目标：4。"), "processing-review gate did not render pending targets");
  assert(els.reviewCompletionGate.textContent.includes("确认问题: 1"), "processing-review gate did not render accepted count");
  assert(els.reviewCompletionGate.textContent.includes("误报: 1"), "processing-review gate did not render false-positive count");
  assert(buildReviewSummary().source_target_count === 6, "filter changed review export target count");
  assert(buildReviewSummary().decisions.length === 6, "filter removed decisions from privacy-safe export");
  assertPublicSafe(JSON.stringify(buildReviewSummary()), "filtered review export summary");
  assertPublicSafe(els.decisionSummary.innerHTML, "review completion summary");
  assertPublicSafe(els.reviewCompletionGate.textContent, "review completion gate");
`, context);
"""

    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as handle:
        handle.write(runner)
        runner_path = Path(handle.name)

    try:
        completed = subprocess.run(
            ["node", str(runner_path)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError:
        return ["Node.js is required for executable review import/export checks but was not found on PATH"]
    finally:
        runner_path.unlink(missing_ok=True)

    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"node exited {completed.returncode}"
        return [f"executable review import/export check failed: {detail}"]
    return []


def validate_workbench(workbench: Path = WORKBENCH) -> dict[str, Any]:
    summary = new_summary(workbench)
    errors = summary["errors"]

    if not workbench.exists():
        add_error(summary, "missing_workbench", f"Missing workbench: {safe_workbench_path(workbench)}")
        return finalize_summary(summary)

    html = workbench.read_text(encoding="utf-8")

    for region in sorted(REQUIRED_REGIONS):
        if f'data-region="{region}"' not in html:
            add_error(summary, "missing_required_region", f"missing data-region={region!r}")

    for required in sorted(REQUIRED_STRINGS):
        if required not in html:
            add_error(summary, "missing_required_string", f"missing required string {required!r}")

    summary["coverage"]["review_acceptance"] = not any(
        error["code"] == "missing_required_string"
        and (
            "review_summary.json" in error["message"]
            or "acceptance_summary.json" in error["message"]
            or "人工复核决定" in error["message"]
        )
        for error in errors
    )

    for label, pattern in FORBIDDEN_PATTERNS.items():
        match = pattern.search(html)
        if match:
            add_error(summary, "forbidden_pattern_found", f"found forbidden {label}")
    summary["privacy"]["forbidden_pattern_checks_passed"] = not any(
        error["code"] == "forbidden_pattern_found" for error in errors
    )

    export_start = html.find('schema: "scan-qc-review-decisions.local.v1"')
    if export_start == -1:
        add_error(summary, "missing_review_export_builder", "missing privacy-safe review export builder")
    else:
        export_block = html[export_start : html.find("function resetReviewState", export_start)]
        for field in sorted(FORBIDDEN_EXPORT_FIELDS | FORBIDDEN_PREVIEW_FIELDS):
            if re.search(rf"\b{re.escape(field)}\b\s*:", export_block):
                add_error(
                    summary,
                    "forbidden_review_export_field",
                    f"review export includes forbidden field {field!r}",
                )
    summary["privacy"]["review_export_forbidden_field_checks_passed"] = not any(
        error["code"] in {"missing_review_export_builder", "forbidden_review_export_field"} for error in errors
    )

    import_start = html.find("function parseReviewDecisionSummary")
    if import_start == -1:
        add_error(summary, "missing_review_import_parser", "missing privacy-safe review import parser")
    else:
        import_block = html[import_start : html.find("function clearPreviewState", import_start)]
        for field in sorted(FORBIDDEN_EXPORT_FIELDS | FORBIDDEN_PREVIEW_FIELDS):
            if re.search(rf"\b{re.escape(field)}\b\s*:", import_block):
                add_error(
                    summary,
                    "forbidden_review_import_field",
                    f"review import reads forbidden field {field!r}",
                )
    summary["privacy"]["review_import_forbidden_field_checks_passed"] = not any(
        error["code"] in {"missing_review_import_parser", "forbidden_review_import_field"} for error in errors
    )

    aggregate_start = html.find("function buildAggregateHandoffModel")
    aggregate_end = html.find("function normalizeStatus", aggregate_start)
    if aggregate_start == -1 or aggregate_end == -1:
        add_error(summary, "missing_aggregate_model_builder", "missing aggregate summary model builder")
    else:
        aggregate_block = html[aggregate_start:aggregate_end]
        for label, required in sorted(REQUIRED_AGGREGATE_FIELDS.items()):
            if required not in aggregate_block:
                add_error(
                    summary,
                    "missing_aggregate_field",
                    f"aggregate summary builder missing {label}: {required!r}",
                )
        for label, required in sorted(REQUIRED_CHECKLIST_FIELDS.items()):
            if required not in aggregate_block:
                add_error(
                    summary,
                    "missing_readiness_field",
                    f"artifact readiness checklist builder missing {label}: {required!r}",
                )
        for label, required in sorted(REQUIRED_COMPATIBILITY_FIELDS.items()):
            if required not in html:
                add_error(
                    summary,
                    "missing_compatibility_field",
                    f"artifact compatibility diagnostics missing {label}: {required!r}",
                )
        required_fragments = {
            "review summary schema classification": 'schema.includes("review-summary")',
            "review summary status-count classification": "payload.status_counts",
            "acceptance summary schema classification": 'schema.includes("acceptance-summary")',
            "acceptance summary pass classification": "payload.pass",
            "acceptance human-review remaining p0": "payload.human_review && payload.human_review.remaining_p0",
            "acceptance human-review remaining p1": "payload.human_review && payload.human_review.remaining_p1",
        }
        for label, fragment in sorted(required_fragments.items()):
            if fragment not in aggregate_block:
                add_error(
                    summary,
                    "missing_aggregate_fragment",
                    f"aggregate summary builder missing {label}: {fragment!r}",
                )
        for label, field in sorted(FORBIDDEN_AGGREGATE_PAYLOAD_FIELDS.items()):
            pattern = rf"\bpayload\.{re.escape(field)}\b|\bpayload\[['\"]{re.escape(field)}['\"]\]"
            if re.search(pattern, aggregate_block):
                add_error(
                    summary,
                    "forbidden_aggregate_payload_field",
                    f"aggregate summary builder reads forbidden {label} field {field!r}",
                )
    summary["coverage"]["aggregate_summary"] = not any(
        error["code"] in {"missing_aggregate_model_builder", "missing_aggregate_field", "missing_aggregate_fragment"}
        for error in errors
    )
    summary["coverage"]["compatibility_diagnostics"] = not any(
        error["code"] == "missing_compatibility_field" for error in errors
    )
    summary["coverage"]["readiness_checklist"] = not any(
        error["code"] == "missing_readiness_field" for error in errors
    )
    summary["privacy"]["aggregate_payload_forbidden_field_checks_passed"] = not any(
        error["code"] == "forbidden_aggregate_payload_field" for error in errors
    )

    demo_start = html.find("const DEMO_FIXTURES = [")
    demo_end = html.find("const els = {", demo_start)
    demo_block = ""
    if demo_start == -1 or demo_end == -1:
        add_error(summary, "missing_demo_fixture_gallery", "missing public-safe demo fixture gallery data")
    else:
        demo_block = html[demo_start:demo_end]
        for label in sorted(REQUIRED_DEMO_FIXTURE_LABELS):
            if label not in demo_block:
                add_error(summary, "missing_demo_fixture_label", f"demo fixture gallery missing label {label!r}")
        for field in sorted(FORBIDDEN_DEMO_FIXTURE_FIELDS):
            if re.search(rf"['\"]{re.escape(field)}['\"]\s*:", demo_block):
                add_error(
                    summary,
                    "forbidden_demo_fixture_field",
                    f"demo fixture gallery includes forbidden field {field!r}",
                )
        if "URL.createObjectURL" in demo_block or "preview" in demo_block.lower():
            add_error(
                summary,
                "demo_fixture_preview_state",
                "demo fixture gallery must not include local preview object URL or filename state",
            )
    summary["coverage"]["demo_fixtures"] = not any(
        error["code"] in {"missing_demo_fixture_gallery", "missing_demo_fixture_label"} for error in errors
    )
    summary["coverage"]["final_handoff_fixtures"] = (
        summary["coverage"]["demo_fixtures"]
        and not any(
            error["code"] == "missing_demo_fixture_label"
            and (
                "通过的最终生产交接" in error["message"]
                or "阻塞的最终生产交接" in error["message"]
            )
            for error in errors
        )
    )
    summary["coverage"]["provider_capability_probe"] = (
        summary["coverage"]["demo_fixtures"]
        and not any(
            error["code"] == "missing_demo_fixture_label"
            and "已禁用的提供方能力探测" in error["message"]
            for error in errors
        )
        and "scan-qc-provider-capability-probe-summary.v1" in demo_block
        and "capability_probe_summary" in demo_block
    )
    summary["privacy"]["demo_fixture_forbidden_field_checks_passed"] = not any(
        error["code"] in {"forbidden_demo_fixture_field", "demo_fixture_preview_state"} for error in errors
    )

    preview_start = html.find("function renderPreview")
    preview_end = html.find("async function loadFile", preview_start)
    if preview_start == -1 or preview_end == -1:
        add_error(summary, "missing_preview_lifecycle_block", "missing local preview lifecycle functions")
    else:
        preview_block = html[preview_start:preview_end]
        for label, required in sorted(REQUIRED_PREVIEW_LIFECYCLE_STRINGS.items()):
            search_area = html if label in {
                "beforeunload revocation",
                "local tab copy",
                "original export exclusion",
                "original slot",
                "preview file input width cap",
                "preview max width cap",
                "preview min width reset",
                "preview slot child width cap",
                "processed export exclusion",
                "processed slot",
            } else preview_block
            if required not in search_area:
                add_error(
                    summary,
                    "missing_preview_lifecycle_string",
                    f"preview lifecycle missing {label}: {required!r}",
                )

    for error in validate_executable_preview_lifecycle(html):
        add_error(summary, "preview_lifecycle_failure", error)
    summary["coverage"]["preview_lifecycle"] = not any(
        error["code"] in {"missing_preview_lifecycle_block", "missing_preview_lifecycle_string", "preview_lifecycle_failure"}
        for error in errors
    )
    summary["privacy"]["preview_lifecycle_public_safe"] = summary["coverage"]["preview_lifecycle"]

    for error in validate_executable_review_decision_import_export(html):
        add_error(summary, "review_import_export_failure", error)
    summary["coverage"]["review_decision_import_export"] = not any(
        error["code"] == "review_import_export_failure" for error in errors
    )

    render_start = html.find("function renderAggregateHandoff")
    render_end = html.find("function workerRange", render_start)
    if render_start == -1 or render_end == -1:
        add_error(summary, "missing_aggregate_renderer", "missing aggregate summary renderer")
    else:
        render_block = html[render_start:render_end]
        expected_labels = {
            "验收通过",
            "阻塞与警告代码",
            "阻塞数",
            "生成时间戳",
            "缺失工件",
            "已省略私有证据",
            "存在/缺失",
            "隐私状态",
            "公开安全工件就绪清单",
            "处理工作线程",
            "提供方能力探测",
            "提供方数",
            "已配置提供方数",
            "去噪点后端摘要",
            "去噪点后端警告代码",
            "有效后端模式",
            "回退数",
            "可见 GPU 数",
            "可见模型数",
            "缺失可选包数",
            "探测隐私状态",
            "请求后端",
            "请求 NumPy 后回退数",
            "公开安全工件兼容性诊断",
            "已识别工件类型",
            "复核状态计数",
            "规则计数",
            "规则状态计数",
            "扫描工作线程",
            "架构版本",
            "架构/类型检测",
        }
        for label in sorted(expected_labels):
            if label not in render_block:
                add_error(summary, "missing_renderer_label", f"aggregate summary renderer missing label {label!r}")

    if "http://" in html or "https://" in html:
        add_error(summary, "external_network_url", "workbench should not depend on external network URLs")

    for error in validate_executable_aggregate_fixtures(html):
        add_error(summary, "executable_fixture_failure", error)
    summary["coverage"]["executable_fixtures"] = not any(
        error["code"] == "executable_fixture_failure" for error in errors
    )

    return finalize_summary(summary)


def finalize_summary(summary: dict[str, Any]) -> dict[str, Any]:
    summary["error_count"] = len(summary["errors"])
    summary["status"] = "pass" if summary["error_count"] == 0 else "fail"
    return summary


def emit_json_summary(summary: dict[str, Any], json_out: Path | None) -> None:
    text = json.dumps(summary, indent=2, sort_keys=True) + "\n"
    if json_out is None:
        print(text, end="")
        return
    json_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(text, encoding="utf-8")


def run_json_self_tests() -> int:
    success = validate_workbench(WORKBENCH)
    if success["status"] != "pass":
        print("JSON self-test failed: expected success status for current workbench.", file=sys.stderr)
        return 1
    if success["error_count"] != 0 or success["errors"]:
        print("JSON self-test failed: success summary included errors.", file=sys.stderr)
        return 1
    required_success_keys = {
        "status",
        "validated_html_path",
        "counts",
        "fixture_groups",
        "coverage",
        "privacy",
        "error_count",
        "errors",
    }
    if set(success) != required_success_keys:
        print("JSON self-test failed: success summary keys changed.", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory(prefix="frontend-workbench-json-self-test-") as temp_dir:
        failure_path = Path(temp_dir) / "missing-workbench.html"
        failure = validate_workbench(failure_path)
    if failure["status"] != "fail" or failure["error_count"] != 1:
        print("JSON self-test failed: expected one synthetic failure.", file=sys.stderr)
        return 1
    if failure["errors"] != [{"code": "missing_workbench", "message": "Missing workbench: missing-workbench.html"}]:
        print("JSON self-test failed: synthetic failure error shape changed.", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory(prefix="frontend-workbench-json-self-test-") as temp_dir:
        unsafe_path = Path(temp_dir) / "unsafe-workbench.html"
        unsafe_path.write_text("<html><script>const leaked = '/Users/example/private_scan.tif blob:synthetic-preview-secret';</script></html>", encoding="utf-8")
        unsafe_failure = validate_workbench(unsafe_path)
    unsafe_serialized = json.dumps(unsafe_failure, sort_keys=True)
    if "blob:synthetic-preview-secret" in unsafe_serialized or "/Users/example/" in unsafe_serialized:
        print("JSON self-test failed: synthetic failure echoed private-looking details.", file=sys.stderr)
        return 1

    json.loads(json.dumps(success, sort_keys=True))
    json.loads(json.dumps(failure, sort_keys=True))
    print("JSON summary self-tests passed.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.self_test_json:
        return run_json_self_tests()

    summary = validate_workbench(args.workbench)

    if args.json or args.json_out is not None:
        emit_json_summary(summary, args.json_out)

    if summary["errors"]:
        print("Frontend workbench validation failed:", file=sys.stderr)
        for error in summary["errors"]:
            print(f"- {error['message']}", file=sys.stderr)
        return 1

    if not args.json:
        print(f"Validated {summary['validated_html_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
