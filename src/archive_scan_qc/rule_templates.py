"""Template catalog and public-safe dry-run summaries."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any

from .rules import (
    BUILTIN_RULE_TEMPLATE_IDS,
    CUSTOM_RULE_TEMPLATE_ID,
    RULE_TEMPLATE_PROCESSING_DEFAULTS,
    RULE_TEMPLATE_VERSION,
    RulesProfileError,
    attach_rule_template,
    builtin_rules_profile,
    processing_defaults_for_rule_template,
    processing_profile_for_rule_template,
    rules_profile_from_mapping,
)


RULE_TEMPLATE_CATALOG_JSON = "rule_template_catalog.json"
RULE_TEMPLATE_DRY_RUN_JSON = "rule_template_dry_run.json"
CATALOG_SCHEMA_VERSION = "scan-qc.rule-template-catalog.v1"
DRY_RUN_SCHEMA_VERSION = "scan-qc.rule-template-dry-run.v1"
CUSTOM_TEMPLATE_VALIDATION_SCHEMA_VERSION = "scan-qc.rule-template-custom-validation.v1"
SERVICE_TEMPLATE_WRITE_SCHEMA_VERSION = "scan-qc.service-rule-template-write.v1"
SERVICE_TEMPLATE_DETAIL_SCHEMA_VERSION = "scan-qc.service-rule-template-detail.v1"
SERVICE_RULE_TEMPLATE_SCHEMA_VERSION = "scan-qc.service-rule-template.v1"
SERVICE_RULE_TEMPLATES_DIRNAME = "rule_templates"
SERVICE_RULE_TEMPLATE_ID_PATTERN = re.compile(r"^custom-[A-Za-z0-9][A-Za-z0-9_-]{2,80}$")
_ALLOWED_CUSTOM_PROCESSING_DEFAULTS = tuple(
    sorted({key for defaults in RULE_TEMPLATE_PROCESSING_DEFAULTS.values() for key in defaults})
)


class RuleTemplateNotFoundError(FileNotFoundError):
    """Raised when a service-managed rule template id has no stored template."""


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
    "ocr-preprocess-light-v1": {
        "name_zh": "OCR 预处理轻量 v1",
        "quality_goal": "生成面向 OCR 的较保守利用副本，提升背景分离、文字对比和可复核质量聚合指标。",
        "intended_inputs": ["纯文本扫描页", "轻噪声 OCR 预处理批次"],
        "risk_boundary": "不是档案保真派生图；照片、印章、批注、表格密集或混合内容必须本地复核。",
        "output_profile": "ocr-preprocess-light",
        "review_policy": "颜色、混合内容、前景损失风险或 OCR 二值化回退页面必须在生产 OCR 前复核。",
    },
    "ocr-preprocess-v1": {
        "name_zh": "OCR 预处理 v1",
        "quality_goal": "生成面向 OCR 的强预处理利用副本，执行背景归一、OCR 去噪、笔画保护和可选二值输出。",
        "intended_inputs": ["纯文本 OCR 批次", "含明显噪声的灰度办公扫描页", "私有验证 OCR 预处理运行"],
        "risk_boundary": "这是 OCR 利用副本，不是保真派生图；进入发布或生产 OCR 前必须通过质量门槛。",
        "output_profile": "ocr-preprocess",
        "review_policy": "颜色、混合内容、前景损失、二值化回退和洗白风险必须在 OCR 生产使用前复核。",
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
    "ocr_preprocess": {"stage": "ocr_preprocessing", "intent": "生成灰度 OCR 预处理利用副本。"},
    "ocr_binary": {"stage": "ocr_preprocessing", "intent": "生成可复核的 OCR 二值旁路副本。"},
    "despeckle_content_type_check": {"stage": "guardrail", "intent": "保护照片和混合内容免受去黑点误处理。"},
    "reuse_scan_measurements": {"stage": "performance", "intent": "复用安全的扫描阶段测量结果。"},
}


def build_rule_template_catalog(
    *,
    service_root: Path | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    templates = [_template_payload(template_id) for template_id in BUILTIN_RULE_TEMPLATE_IDS]
    templates.append(_custom_template_payload())
    if service_root is not None:
        templates.extend(_stored_template_catalog_payloads(service_root))
    return {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "generated_at": generated_at or _utc_now(),
        "status": "pass",
        "aggregate_only": True,
        "public_safe": True,
        "templates": templates,
        "privacy": _privacy_payload(reads_scan_report=False),
    }


def build_rule_template_detail(
    *,
    template_id: str,
    service_root: Path | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    if service_root is not None and _is_service_rule_template_id(template_id):
        stored = load_service_rule_template(service_root, template_id)
        return _stored_template_detail_payload(stored, generated_at=generated_at)
    return build_rule_template_dry_run(rule_template=template_id, generated_at=generated_at)


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


def build_custom_rule_template_validation(
    *,
    template_draft: dict[str, Any],
    generated_at: str | None = None,
) -> dict[str, Any]:
    profile = attach_rule_template(
        rules_profile_from_mapping(template_draft, source="service-inline-template-draft"),
        CUSTOM_RULE_TEMPLATE_ID,
    )
    rule_settings = profile.rules
    quality_thresholds = profile.threshold_summary()["quality"]
    disabled_rules = sum(1 for setting in rule_settings.values() if not setting.enabled)
    severity_overrides = sum(1 for setting in rule_settings.values() if setting.severity is not None)
    return {
        "schema_version": CUSTOM_TEMPLATE_VALIDATION_SCHEMA_VERSION,
        "generated_at": generated_at or _utc_now(),
        "status": "pass",
        "valid": True,
        "aggregate_only": True,
        "public_safe": True,
        "derivative_images_written": False,
        "template": {
            "id": CUSTOM_RULE_TEMPLATE_ID,
            "schema_version": RULE_TEMPLATE_VERSION,
            "stable": False,
            "customizable": True,
        },
        "validation": {
            "rule_count": len(rule_settings),
            "disabled_rule_count": disabled_rules,
            "severity_override_count": severity_overrides,
            "min_dpi": profile.min_dpi,
            "dpi_purpose": profile.dpi_purpose,
            "effective_min_dpi": profile.effective_min_dpi(),
            "quality_threshold_count": len(quality_thresholds),
        },
        "risk_codes": _custom_validation_warnings(profile),
        "privacy": _privacy_payload(reads_scan_report=False),
    }


def save_service_rule_template(
    *,
    service_root: Path,
    template_id: str,
    template_draft: dict[str, Any],
    replace_existing: bool = False,
    generated_at: str | None = None,
) -> dict[str, Any]:
    service_template_id = _validate_service_rule_template_id(template_id)
    validation = build_custom_rule_template_validation(
        template_draft=template_draft,
        generated_at=generated_at,
    )
    processing_defaults = _custom_processing_defaults(template_draft)
    path = _service_rule_template_path(service_root, service_template_id)
    existed = path.is_file()
    if existed and not replace_existing:
        raise FileExistsError("Service rule template already exists.")
    path.parent.mkdir(parents=True, exist_ok=True)
    stored = {
        "schema_version": SERVICE_RULE_TEMPLATE_SCHEMA_VERSION,
        "service_template_id": service_template_id,
        "base_template_id": CUSTOM_RULE_TEMPLATE_ID,
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
        "local_only": True,
        "template": template_draft,
        "processing_defaults": processing_defaults,
        "validation": {
            "schema_version": validation["schema_version"],
            "validation": validation["validation"],
            "risk_codes": validation["risk_codes"],
        },
    }
    if existed:
        previous = _read_service_rule_template(path)
        if isinstance(previous, dict) and isinstance(previous.get("created_at"), str):
            stored["created_at"] = previous["created_at"]
    path.write_text(json.dumps(stored, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return _service_rule_template_write_payload(
        stored,
        action="updated" if existed else "created",
        generated_at=generated_at,
    )


def load_service_rule_template(service_root: Path, template_id: str) -> dict[str, Any]:
    service_template_id = _validate_service_rule_template_id(template_id)
    path = _service_rule_template_path(service_root, service_template_id)
    if not path.is_file():
        raise RuleTemplateNotFoundError("Service rule template does not exist.")
    stored = _read_service_rule_template(path)
    if not isinstance(stored, dict) or stored.get("schema_version") != SERVICE_RULE_TEMPLATE_SCHEMA_VERSION:
        raise RulesProfileError("Service rule template schema is unsupported.")
    if stored.get("service_template_id") != service_template_id:
        raise RulesProfileError("Service rule template id mismatch.")
    return stored


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


def _stored_template_catalog_payloads(service_root: Path) -> list[dict[str, Any]]:
    templates_dir = _service_rule_templates_dir(service_root)
    if not templates_dir.is_dir():
        return []
    templates: list[dict[str, Any]] = []
    for path in sorted(templates_dir.glob("*.json"), key=lambda item: item.name):
        stored = _read_service_rule_template(path)
        if isinstance(stored, dict) and stored.get("schema_version") == SERVICE_RULE_TEMPLATE_SCHEMA_VERSION:
            templates.append(_stored_template_summary_payload(stored))
    return templates


def _stored_template_summary_payload(stored: dict[str, Any]) -> dict[str, Any]:
    validation = stored.get("validation") if isinstance(stored.get("validation"), dict) else {}
    validation_counts = validation.get("validation") if isinstance(validation.get("validation"), dict) else {}
    risk_codes = validation.get("risk_codes") if isinstance(validation.get("risk_codes"), list) else []
    return {
        "id": str(stored.get("service_template_id") or ""),
        "schema_version": RULE_TEMPLATE_VERSION,
        "base_template_id": CUSTOM_RULE_TEMPLATE_ID,
        "service_managed": True,
        "stable": False,
        "customizable": True,
        "quality_goal": "service-managed custom template",
        "output_profile": "custom",
        "review_policy": "custom templates require local review before production",
        "validation": _safe_validation_counts(validation_counts),
        "risk_codes": [str(code) for code in risk_codes if isinstance(code, str)],
        "processing_defaults": _bool_dict(stored.get("processing_defaults")),
    }


def _stored_template_detail_payload(stored: dict[str, Any], *, generated_at: str | None) -> dict[str, Any]:
    summary = _stored_template_summary_payload(stored)
    return {
        "schema_version": SERVICE_TEMPLATE_DETAIL_SCHEMA_VERSION,
        "generated_at": generated_at or _utc_now(),
        "status": "pass",
        "aggregate_only": True,
        "public_safe": True,
        "derivative_images_written": False,
        "template": summary,
        "planned_operations": _planned_operations_from_defaults(summary["processing_defaults"]),
        "risk_codes": summary["risk_codes"],
        "privacy": _privacy_payload(reads_scan_report=False),
    }


def _service_rule_template_write_payload(
    stored: dict[str, Any],
    *,
    action: str,
    generated_at: str | None,
) -> dict[str, Any]:
    summary = _stored_template_summary_payload(stored)
    return {
        "schema_version": SERVICE_TEMPLATE_WRITE_SCHEMA_VERSION,
        "generated_at": generated_at or _utc_now(),
        "status": "pass",
        "action": action,
        "template": summary,
        "storage": {
            "managed_by_service": True,
            "path_returned": False,
            "local_only_payload_written": True,
        },
        "privacy": _privacy_payload(reads_scan_report=False),
    }


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
        "processing_profile": processing_profile_for_rule_template(template_id),
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
        "processing_profile": "standard",
        "stable": False,
        "customizable": True,
    }


def _planned_operations(template_id: str) -> list[dict[str, Any]]:
    defaults = processing_defaults_for_rule_template(template_id)
    return _planned_operations_from_defaults(defaults)


def _planned_operations_from_defaults(defaults: dict[str, bool]) -> list[dict[str, Any]]:
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


def _custom_processing_defaults(template_draft: dict[str, Any]) -> dict[str, bool]:
    raw_defaults = template_draft.get("processing_defaults", {})
    if raw_defaults is None:
        raw_defaults = {}
    if not isinstance(raw_defaults, dict):
        raise RulesProfileError("Custom template field 'processing_defaults' must be an object.")
    defaults: dict[str, bool] = {}
    allowed = set(_ALLOWED_CUSTOM_PROCESSING_DEFAULTS)
    for key, value in raw_defaults.items():
        if not isinstance(key, str) or key not in allowed:
            raise RulesProfileError(f"Custom template processing default '{key}' is not supported.")
        if not isinstance(value, bool):
            raise RulesProfileError(f"Custom template processing default '{key}' must be a boolean.")
        defaults[key] = value
    return defaults


def _validate_service_rule_template_id(template_id: str) -> str:
    if not isinstance(template_id, str) or not SERVICE_RULE_TEMPLATE_ID_PATTERN.match(template_id):
        raise RulesProfileError("Service rule template id must start with 'custom-' and contain only safe characters.")
    if template_id in BUILTIN_RULE_TEMPLATE_IDS or template_id == CUSTOM_RULE_TEMPLATE_ID:
        raise RulesProfileError("Service rule template id must not collide with built-in templates.")
    return template_id


def _is_service_rule_template_id(template_id: str) -> bool:
    return isinstance(template_id, str) and bool(SERVICE_RULE_TEMPLATE_ID_PATTERN.match(template_id))


def _service_rule_template_path(service_root: Path, template_id: str) -> Path:
    templates_dir = _service_rule_templates_dir(service_root)
    path = (templates_dir / f"{template_id}.json").resolve()
    if path.parent != templates_dir:
        raise RulesProfileError("Service rule template path escapes the template directory.")
    return path


def _service_rule_templates_dir(service_root: Path) -> Path:
    return (service_root.resolve() / SERVICE_RULE_TEMPLATES_DIRNAME).resolve()


def _read_service_rule_template(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _bool_dict(value: Any) -> dict[str, bool]:
    if not isinstance(value, dict):
        return {}
    return {str(key): bool(flag) for key, flag in sorted(value.items()) if isinstance(key, str)}


def _safe_validation_counts(value: Any) -> dict[str, int | str]:
    if not isinstance(value, dict):
        return {}
    public_fields = (
        "rule_count",
        "disabled_rule_count",
        "severity_override_count",
        "min_dpi",
        "dpi_purpose",
        "effective_min_dpi",
        "quality_threshold_count",
    )
    payload: dict[str, int | str] = {}
    for field in public_fields:
        raw = value.get(field)
        if isinstance(raw, str):
            payload[field] = raw
        elif isinstance(raw, int):
            payload[field] = _safe_int(raw)
    return payload


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
    if rule_template in {"ocr-preprocess-light-v1", "ocr-preprocess-v1"}:
        warnings.append("ocr_preprocess_not_archival_fidelity_derivative")
        warnings.append("ocr_preprocess_requires_private_quality_gate")
        warnings.append("mixed_photo_stamp_content_requires_review")
    if rule_template == "ocr-preprocess-v1":
        warnings.append("ocr_binary_output_requires_review_gate")
    if rule_template in {"high-fidelity-original", "photo-mixed-safe-v1"}:
        warnings.append("strong_cleanup_disabled_by_high_fidelity_goal")
    return warnings


def _custom_validation_warnings(profile) -> list[str]:  # type: ignore[no-untyped-def]
    warnings = ["custom_template_requires_local_review_before_production"]
    if profile.template_id != CUSTOM_RULE_TEMPLATE_ID:
        warnings.append("custom_template_id_was_normalized")
    if profile.effective_min_dpi() < 300:
        warnings.append("custom_template_effective_dpi_below_print_recommendation")
    if not profile.rules:
        warnings.append("custom_template_has_no_rule_overrides")
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
