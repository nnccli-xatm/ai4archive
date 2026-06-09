"""Template catalog and public-safe dry-run summaries."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from .rules import (
    BUILTIN_RULE_TEMPLATE_IDS,
    CUSTOM_RULE_TEMPLATE_ID,
    RULE_TEMPLATE_VERSION,
    RulesProfileError,
    builtin_rules_profile,
    processing_defaults_for_rule_template,
)


RULE_TEMPLATE_CATALOG_JSON = "rule_template_catalog.json"
RULE_TEMPLATE_DRY_RUN_JSON = "rule_template_dry_run.json"
CATALOG_SCHEMA_VERSION = "scan-qc.rule-template-catalog.v1"
DRY_RUN_SCHEMA_VERSION = "scan-qc.rule-template-dry-run.v1"

_TEMPLATE_DESCRIPTIONS: dict[str, dict[str, Any]] = {
    "dat-31-2017-standard": {
        "name_zh": "DA/T 31-2017 标准模板",
        "quality_goal": "档案验收安全优先，保守生成可复核利用副本。",
        "intended_inputs": ["普通纸质档案扫描件", "需要保留原貌的标准批次"],
        "risk_boundary": "不追求强清洁；低置信度处理必须跳过或进入复核。",
        "output_profile": "archival-safe",
        "review_policy": "发现 P0、处理失败或 guardrail 命中时进入人工复核。",
    },
    "archival-safe-v1": {
        "name_zh": "档案安全 v1",
        "quality_goal": "保留原貌优先，只做低风险几何、边框和极小噪点处理。",
        "intended_inputs": ["普通纸质档案扫描件", "需要保留原貌的标准批次"],
        "risk_boundary": "不追求强清洁；低置信度处理必须跳过或进入复核。",
        "output_profile": "archival-safe",
        "review_policy": "发现 P0、处理失败或 guardrail 命中时进入人工复核。",
    },
    "text-clean-print": {
        "name_zh": "纯文本清洁打印模板",
        "quality_goal": "提升纯文本扫描件洁净度、背景均匀度和文字可读性。",
        "intended_inputs": ["确认无照片和复杂彩色内容的纯文本页", "用于打印或高可读利用副本的批次"],
        "risk_boundary": "必须确认批次以文字为主；印章、批注、照片和混合内容需要人工复核。",
        "output_profile": "text-clean-readable",
        "review_policy": "强清洁、文字增强、扫描线和透印处理后按风险分组复核。",
    },
    "text-clean-readable-v1": {
        "name_zh": "文本清晰可读 v1",
        "quality_goal": "提升纯文本扫描件背景均匀度、文字对比、扫描线和透印控制。",
        "intended_inputs": ["确认无照片和复杂彩色内容的纯文本页", "用于高可读利用副本的批次"],
        "risk_boundary": "必须确认批次以文字为主；印章、批注、照片和混合内容需要人工复核。",
        "output_profile": "text-clean-readable",
        "review_policy": "背景清理、文字增强、扫描线和透印处理后按风险分组复核。",
    },
    "print-clean-v1": {
        "name_zh": "打印清洁 v1",
        "quality_goal": "面向后续打印或利用副本，允许更强的背景均衡和文字锐化。",
        "intended_inputs": ["确认纯文本的打印利用副本", "需要最大化可读性的低对比文字批次"],
        "risk_boundary": "不用于照片、印章密集、批注密集或珍贵原貌材料；过处理必须复核。",
        "output_profile": "print-clean",
        "review_policy": "默认要求关注过处理、过锐化、背景洗白和文字断裂风险。",
    },
    "high-fidelity-original": {
        "name_zh": "高保真原貌模板",
        "quality_goal": "尽量保留照片、绘画、印章、批注和历史纸张原貌。",
        "intended_inputs": ["照片", "绘画", "珍贵档案", "有大量印章或彩色批注的混合页"],
        "risk_boundary": "核心内容区域默认不做清洁；只允许边框、扫描台边等低风险处理。",
        "output_profile": "photo-mixed-safe",
        "review_policy": "任何强清洁需求都应复制为自定义模板并经过 dry-run 复核。",
    },
    "photo-mixed-safe-v1": {
        "name_zh": "照片混排保护 v1",
        "quality_goal": "照片、图像、印章、批注和彩色区域保护优先，只允许低风险边界处理。",
        "intended_inputs": ["照片", "图文混排页", "印章或批注较多的页面", "珍贵原貌材料"],
        "risk_boundary": "核心内容区域默认不做清洁；任何强清洁需求都应转自定义模板并 dry-run。",
        "output_profile": "photo-mixed-safe",
        "review_policy": "任何强清洁、文字增强或去透印请求都需要人工复核。",
    },
}

_OPERATION_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "auto_crop": {"stage": "geometry", "intent": "裁掉可信空白页边。"},
    "deskew": {"stage": "geometry", "intent": "保守校正小角度倾斜。"},
    "trim_dark_border": {"stage": "geometry", "intent": "清理扫描黑边。"},
    "scanner_gutter_trim": {"stage": "geometry", "intent": "裁掉窄幅浅灰扫描台边。"},
    "despeckle": {"stage": "defect_cleanup", "intent": "清理孤立黑点。"},
    "normalize_tones": {"stage": "background", "intent": "改善灰底或偏暗低对比文字页。"},
    "normalize_paper_color_cast": {"stage": "background", "intent": "校正轻微统一纸面偏色。"},
    "lighten_edge_shadow": {"stage": "background", "intent": "减淡窄幅页边阴影。"},
    "lighten_corner_shadows": {"stage": "background", "intent": "减淡平滑角落阴影。"},
    "lighten_background_stains": {"stage": "background", "intent": "减淡浅色背景小污渍。"},
    "lighten_fold_shadows": {"stage": "background", "intent": "减淡干净背景中的折痕阴影。"},
    "level_illumination_gradient": {"stage": "background", "intent": "校平轻微扫描明暗渐变。"},
    "clean_bleed_through": {"stage": "defect_cleanup", "intent": "保守弱化浅色背景透印。"},
    "lighten_scanlines": {"stage": "defect_cleanup", "intent": "弱化低对比扫描线。"},
    "enhance_faded_text": {"stage": "text_enhancement", "intent": "增强低对比浅墨正文。"},
    "sharpen_text_edges": {"stage": "text_enhancement", "intent": "轻量增强文字边缘清晰度。"},
    "despeckle_content_type_check": {"stage": "guardrail", "intent": "保护照片和混合内容免受去黑点误处理。"},
    "reuse_scan_measurements": {"stage": "performance", "intent": "复用安全的扫描阶段测量结果。"},
}


def build_rule_template_catalog(*, generated_at: str | None = None) -> dict[str, Any]:
    templates = [_template_payload(template_id) for template_id in BUILTIN_RULE_TEMPLATE_IDS]
    templates.append(_custom_template_payload())
    return {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "generated_at": generated_at or _utc_now(),
        "status": "pass",
        "aggregate_only": True,
        "public_safe": True,
        "templates": templates,
        "privacy": _privacy_payload(reads_scan_report=False),
    }


def build_rule_template_dry_run(
    *,
    rule_template: str,
    scan_report: dict[str, Any] | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    if rule_template == CUSTOM_RULE_TEMPLATE_ID:
        raise RulesProfileError("rule-template dry-run for custom templates requires a validated rules profile path.")
    template = _template_payload(rule_template)
    scan_summary = _scan_summary(scan_report)
    warnings = _dry_run_warnings(rule_template, scan_summary, scan_report_provided=scan_report is not None)
    return {
        "schema_version": DRY_RUN_SCHEMA_VERSION,
        "generated_at": generated_at or _utc_now(),
        "status": "pass",
        "aggregate_only": True,
        "public_safe": True,
        "derivative_images_written": False,
        "template": template,
        "scan_summary": scan_summary,
        "planned_operations": _planned_operations(rule_template),
        "risk_codes": warnings,
        "review_policy": template["review_policy"],
        "privacy": _privacy_payload(reads_scan_report=scan_report is not None),
    }


def load_scan_report(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Scan report JSON must be an object.")
    return payload


def write_rule_template_catalog(payload: dict[str, Any], output_path: Path) -> Path:
    return _write_json(payload, output_path, RULE_TEMPLATE_CATALOG_JSON)


def write_rule_template_dry_run(payload: dict[str, Any], output_path: Path) -> Path:
    return _write_json(payload, output_path, RULE_TEMPLATE_DRY_RUN_JSON)


def _template_payload(template_id: str) -> dict[str, Any]:
    profile = builtin_rules_profile(template_id)
    metadata = profile.metadata()
    description = _TEMPLATE_DESCRIPTIONS[template_id]
    return {
        "id": template_id,
        "schema_version": RULE_TEMPLATE_VERSION,
        "name_zh": description["name_zh"],
        "quality_goal": description["quality_goal"],
        "intended_inputs": description["intended_inputs"],
        "risk_boundary": description["risk_boundary"],
        "output_profile": description["output_profile"],
        "review_policy": description["review_policy"],
        "thresholds": metadata["thresholds"],
        "processing_defaults": processing_defaults_for_rule_template(template_id),
        "stable": True,
        "customizable": False,
    }


def _custom_template_payload() -> dict[str, Any]:
    return {
        "id": CUSTOM_RULE_TEMPLATE_ID,
        "schema_version": RULE_TEMPLATE_VERSION,
        "name_zh": "自定义模板",
        "quality_goal": "由项目配置定义，但必须保留源文件安全、隐私边界和 guardrail。",
        "intended_inputs": ["项目自定义批次"],
        "risk_boundary": "必须通过 schema 校验和 dry-run 后才能用于正式批次。",
        "output_profile": "custom",
        "review_policy": "按自定义模板风险策略和系统强制 guardrail 复核。",
        "thresholds": {},
        "processing_defaults": {},
        "stable": False,
        "customizable": True,
    }


def _planned_operations(template_id: str) -> list[dict[str, Any]]:
    defaults = processing_defaults_for_rule_template(template_id)
    planned = []
    for operation, enabled in sorted(defaults.items()):
        description = _OPERATION_DESCRIPTIONS.get(operation, {"stage": "other", "intent": operation})
        planned.append(
            {
                "operation": operation,
                "enabled": bool(enabled),
                "stage": description["stage"],
                "intent_zh": description["intent"],
                "writes_derivative_pixels": bool(enabled and operation not in {"reuse_scan_measurements", "despeckle_content_type_check"}),
            }
        )
    return planned


def _scan_summary(scan_report: dict[str, Any] | None) -> dict[str, Any]:
    summary = scan_report.get("summary", {}) if isinstance(scan_report, dict) else {}
    manifest = scan_report.get("manifest", {}) if isinstance(scan_report, dict) else {}
    return {
        "provided": scan_report is not None,
        "total_files": _safe_int(summary.get("total_files")),
        "openable_files": _safe_int(summary.get("openable_files")),
        "p0_findings": _safe_int(summary.get("p0_findings")),
        "p1_findings": _safe_int(summary.get("p1_findings")),
        "p2_findings": _safe_int(summary.get("p2_findings")),
        "total_findings": _safe_int(summary.get("total_findings")),
        "source_rules_template_id": _source_template_id(manifest),
    }


def _source_template_id(manifest: Any) -> str | None:
    if not isinstance(manifest, dict):
        return None
    rules_profile = manifest.get("rules_profile")
    if not isinstance(rules_profile, dict):
        return None
    template = rules_profile.get("template")
    if not isinstance(template, dict):
        return None
    template_id = template.get("id")
    if not isinstance(template_id, str):
        return None
    known_template_ids = set(BUILTIN_RULE_TEMPLATE_IDS)
    known_template_ids.add(CUSTOM_RULE_TEMPLATE_ID)
    return template_id if template_id in known_template_ids else "unknown_or_custom"


def _dry_run_warnings(rule_template: str, scan_summary: dict[str, Any], *, scan_report_provided: bool) -> list[str]:
    warnings: list[str] = []
    if not scan_report_provided:
        warnings.append("scan_report_not_provided")
    if scan_summary["total_files"] == 0 and scan_report_provided:
        warnings.append("scan_report_has_no_files")
    if scan_summary["p0_findings"] > 0:
        warnings.append("p0_findings_require_review_before_processing")
    if rule_template in {"text-clean-print", "text-clean-readable-v1", "print-clean-v1"}:
        warnings.append("text_clean_requires_pure_text_batch_confirmation")
        warnings.append("mixed_photo_stamp_content_requires_review")
    if rule_template == "print-clean-v1":
        warnings.append("print_clean_requires_overprocessing_review")
    if rule_template in {"high-fidelity-original", "photo-mixed-safe-v1"}:
        warnings.append("strong_cleanup_disabled_by_high_fidelity_goal")
    return warnings


def _privacy_payload(*, reads_scan_report: bool) -> dict[str, bool]:
    return {
        "aggregate_only": True,
        "public_safe": True,
        "reads_scan_report": reads_scan_report,
        "derivative_images_written": False,
        "contains_file_list": False,
        "contains_paths": False,
        "contains_filenames": False,
        "contains_hashes": False,
        "contains_thumbnails": False,
        "contains_ocr_text": False,
        "contains_image_content": False,
        "contains_environment_values": False,
        "contains_row_level_evidence": False,
    }


def _write_json(payload: dict[str, Any], output_path: Path, default_name: str) -> Path:
    path = output_path / default_name if output_path.suffix == "" else output_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _safe_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
