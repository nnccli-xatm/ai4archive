"""Rules profile loading and validation for scan QC."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
from pathlib import Path
from typing import Any


VALID_SEVERITIES = {"P0", "P1", "P2"}
VALID_DPI_PURPOSES = {"standard", "com", "reproduction", "print"}
RULE_TEMPLATE_VERSION = "scan-qc.rule-template.v1"
CUSTOM_RULE_TEMPLATE_ID = "custom"
BUILTIN_RULE_TEMPLATE_IDS = (
    "dat-31-2017-standard",
    "archival-safe-v1",
    "text-clean-print",
    "text-clean-readable-v1",
    "print-clean-v1",
    "high-fidelity-original",
    "photo-mixed-safe-v1",
)
RULE_TEMPLATE_IDS = (*BUILTIN_RULE_TEMPLATE_IDS, CUSTOM_RULE_TEMPLATE_ID)
ARCHIVAL_SAFE_PROCESSING_DEFAULTS = {
    "auto_crop": True,
    "deskew": True,
    "trim_dark_border": True,
    "despeckle": True,
    "reuse_scan_measurements": True,
}
TEXT_CLEAN_PROCESSING_DEFAULTS = {
    "auto_crop": True,
    "deskew": True,
    "trim_dark_border": True,
    "scanner_gutter_trim": True,
    "despeckle": True,
    "despeckle_content_type_check": False,
    "normalize_tones": True,
    "normalize_paper_color_cast": True,
    "lighten_edge_shadow": True,
    "lighten_corner_shadows": True,
    "lighten_background_stains": True,
    "lighten_fold_shadows": True,
    "level_illumination_gradient": True,
    "clean_bleed_through": True,
    "lighten_scanlines": True,
    "enhance_faded_text": True,
    "sharpen_text_edges": True,
    "reuse_scan_measurements": True,
}
PHOTO_MIXED_SAFE_PROCESSING_DEFAULTS = {
    "trim_dark_border": True,
    "scanner_gutter_trim": True,
    "reuse_scan_measurements": True,
}
RULE_TEMPLATE_PROCESSING_DEFAULTS: dict[str, dict[str, bool]] = {
    "dat-31-2017-standard": {
        **ARCHIVAL_SAFE_PROCESSING_DEFAULTS,
    },
    "archival-safe-v1": {
        **ARCHIVAL_SAFE_PROCESSING_DEFAULTS,
    },
    "text-clean-print": {
        **TEXT_CLEAN_PROCESSING_DEFAULTS,
    },
    "text-clean-readable-v1": {
        **TEXT_CLEAN_PROCESSING_DEFAULTS,
    },
    "print-clean-v1": {
        **TEXT_CLEAN_PROCESSING_DEFAULTS,
    },
    "high-fidelity-original": {
        **PHOTO_MIXED_SAFE_PROCESSING_DEFAULTS,
    },
    "photo-mixed-safe-v1": {
        **PHOTO_MIXED_SAFE_PROCESSING_DEFAULTS,
    },
    CUSTOM_RULE_TEMPLATE_ID: {},
}
RULE_TEMPLATE_PROCESSING_PROFILES: dict[str, str] = {
    "dat-31-2017-standard": "standard",
    "archival-safe-v1": "standard",
    "text-clean-print": "standard",
    "text-clean-readable-v1": "standard",
    "print-clean-v1": "print_clean",
    "high-fidelity-original": "standard",
    "photo-mixed-safe-v1": "standard",
    CUSTOM_RULE_TEMPLATE_ID: "standard",
}
DPI_MINIMUM_BY_PURPOSE: dict[str, int] = {
    "standard": 200,
    "com": 300,
    "reproduction": 600,
    "print": 300,
}


class RulesProfileError(ValueError):
    """Raised when a rules profile cannot be loaded or validated."""


@dataclass(frozen=True)
class RuleSetting:
    enabled: bool = True
    severity: str | None = None


@dataclass(frozen=True)
class RulesProfile:
    name: str = "default"
    version: str = "scan-qc.phase1.v1"
    source: str = "builtin"
    template_id: str | None = None
    template_version: str | None = None
    template_source: str | None = None
    min_dpi: int = 200
    dpi_purpose: str = "standard"
    name_pattern: str | None = None
    dark_mean_threshold: float = 45.0
    bright_mean_threshold: float = 250.0
    low_contrast_stddev_threshold: float = 10.0
    blur_laplacian_variance_threshold: float = 20.0
    blur_min_contrast_stddev: float = 12.0
    blank_brightness_min: float = 248.0
    blank_contrast_max: float = 6.0
    blank_foreground_coverage_max: float = 0.003
    blank_edge_coverage_max: float = 0.002
    blank_dark_pixel_ratio_max: float = 0.0005
    despeckle_max_pixel_change_ratio: float = 0.01
    deskew_residual_threshold: float = 0.5
    rules: dict[str, RuleSetting] = field(default_factory=dict)

    def effective_min_dpi(self) -> int:
        purpose_dpi = DPI_MINIMUM_BY_PURPOSE.get(self.dpi_purpose, self.min_dpi)
        return max(purpose_dpi, self.min_dpi)

    def is_rule_enabled(self, rule: str) -> bool:
        return self.rules.get(rule, RuleSetting()).enabled

    def severity_for(self, rule: str, default: str) -> str:
        return self.rules.get(rule, RuleSetting()).severity or default

    def threshold_summary(self) -> dict[str, Any]:
        return {
            "min_dpi": self.min_dpi,
            "dpi_purpose": self.dpi_purpose,
            "effective_min_dpi": self.effective_min_dpi(),
            "name_pattern": self.name_pattern,
            "quality": {
                "dark_mean_threshold": self.dark_mean_threshold,
                "bright_mean_threshold": self.bright_mean_threshold,
                "low_contrast_stddev_threshold": self.low_contrast_stddev_threshold,
                "blur_laplacian_variance_threshold": self.blur_laplacian_variance_threshold,
                "blur_min_contrast_stddev": self.blur_min_contrast_stddev,
                "blank_brightness_min": self.blank_brightness_min,
                "blank_contrast_max": self.blank_contrast_max,
                "blank_foreground_coverage_max": self.blank_foreground_coverage_max,
                "blank_edge_coverage_max": self.blank_edge_coverage_max,
                "blank_dark_pixel_ratio_max": self.blank_dark_pixel_ratio_max,
                "despeckle_max_pixel_change_ratio": self.despeckle_max_pixel_change_ratio,
                "deskew_residual_threshold": self.deskew_residual_threshold,
            },
        }

    def metadata(self) -> dict[str, Any]:
        payload = {
            "name": self.name,
            "version": self.version,
            "source": self.source,
            "thresholds": self.threshold_summary(),
            "rules": {
                rule: {"enabled": setting.enabled, "severity": setting.severity}
                for rule, setting in sorted(self.rules.items())
            },
        }
        if self.template_id:
            payload["template"] = {
                "id": self.template_id,
                "version": self.template_version or self.version,
                "source": self.template_source or self.source,
                "processing_defaults": processing_defaults_for_rule_template(self.template_id),
            }
        return payload


def default_rules_profile() -> RulesProfile:
    return RulesProfile()


def builtin_rules_profile(template_id: str) -> RulesProfile:
    if template_id in {"dat-31-2017-standard", "archival-safe-v1"}:
        return RulesProfile(
            name=template_id,
            version=RULE_TEMPLATE_VERSION,
            source=f"builtin-template:{template_id}",
            template_id=template_id,
            template_version=RULE_TEMPLATE_VERSION,
            template_source="builtin",
            min_dpi=200,
            dpi_purpose="standard",
            despeckle_max_pixel_change_ratio=0.008,
            deskew_residual_threshold=0.5,
        )
    if template_id in {"text-clean-print", "text-clean-readable-v1", "print-clean-v1"}:
        return RulesProfile(
            name=template_id,
            version=RULE_TEMPLATE_VERSION,
            source=f"builtin-template:{template_id}",
            template_id=template_id,
            template_version=RULE_TEMPLATE_VERSION,
            template_source="builtin",
            min_dpi=300,
            dpi_purpose="print",
            dark_mean_threshold=35.0,
            bright_mean_threshold=252.0,
            low_contrast_stddev_threshold=8.0,
            blur_laplacian_variance_threshold=24.0,
            blur_min_contrast_stddev=10.0,
            blank_brightness_min=250.0,
            blank_contrast_max=5.0,
            blank_foreground_coverage_max=0.002,
            blank_edge_coverage_max=0.0015,
            blank_dark_pixel_ratio_max=0.0003,
            despeckle_max_pixel_change_ratio=0.02,
            deskew_residual_threshold=0.4,
        )
    if template_id in {"high-fidelity-original", "photo-mixed-safe-v1"}:
        return RulesProfile(
            name=template_id,
            version=RULE_TEMPLATE_VERSION,
            source=f"builtin-template:{template_id}",
            template_id=template_id,
            template_version=RULE_TEMPLATE_VERSION,
            template_source="builtin",
            min_dpi=300,
            dpi_purpose="reproduction",
            dark_mean_threshold=55.0,
            bright_mean_threshold=248.0,
            low_contrast_stddev_threshold=12.0,
            blur_laplacian_variance_threshold=18.0,
            blur_min_contrast_stddev=14.0,
            despeckle_max_pixel_change_ratio=0.003,
            deskew_residual_threshold=0.3,
        )
    raise RulesProfileError(
        f"Unknown rule template '{template_id}'. Expected one of {', '.join(RULE_TEMPLATE_IDS)}."
    )


def attach_rule_template(profile: RulesProfile, template_id: str) -> RulesProfile:
    if template_id not in RULE_TEMPLATE_IDS:
        raise RulesProfileError(
            f"Unknown rule template '{template_id}'. Expected one of {', '.join(RULE_TEMPLATE_IDS)}."
        )
    return replace(
        profile,
        template_id=template_id,
        template_version=RULE_TEMPLATE_VERSION,
        template_source="custom-file" if template_id == CUSTOM_RULE_TEMPLATE_ID else "builtin",
    )


def processing_defaults_for_rule_template(template_id: str | None) -> dict[str, bool]:
    if not template_id:
        return {}
    if template_id not in RULE_TEMPLATE_IDS:
        raise RulesProfileError(
            f"Unknown rule template '{template_id}'. Expected one of {', '.join(RULE_TEMPLATE_IDS)}."
        )
    return dict(RULE_TEMPLATE_PROCESSING_DEFAULTS[template_id])


def processing_profile_for_rule_template(template_id: str | None) -> str:
    if not template_id:
        return "standard"
    if template_id not in RULE_TEMPLATE_IDS:
        raise RulesProfileError(
            f"Unknown rule template '{template_id}'. Expected one of {', '.join(RULE_TEMPLATE_IDS)}."
        )
    return RULE_TEMPLATE_PROCESSING_PROFILES[template_id]


def load_rules_profile_selection(
    rules_profile_path: Path | None,
    rule_template_id: str | None,
) -> RulesProfile | None:
    if not rule_template_id:
        return load_rules_profile(rules_profile_path) if rules_profile_path else None
    if rule_template_id in BUILTIN_RULE_TEMPLATE_IDS:
        if rules_profile_path is not None:
            raise RulesProfileError(
                "Built-in rule templates cannot be combined with --rules-profile. "
                "Use --rule-template custom with --rules-profile for custom templates."
            )
        return builtin_rules_profile(rule_template_id)
    if rule_template_id == CUSTOM_RULE_TEMPLATE_ID:
        if rules_profile_path is None:
            raise RulesProfileError("--rule-template custom requires --rules-profile.")
        return attach_rule_template(load_rules_profile(rules_profile_path), CUSTOM_RULE_TEMPLATE_ID)
    raise RulesProfileError(
        f"Unknown rule template '{rule_template_id}'. Expected one of {', '.join(RULE_TEMPLATE_IDS)}."
    )


def load_rules_profile(path: Path) -> RulesProfile:
    if not path.exists():
        raise RulesProfileError(f"Rules profile does not exist: {path}")
    if not path.is_file():
        raise RulesProfileError(f"Rules profile is not a file: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RulesProfileError(f"Rules profile JSON is invalid at line {exc.lineno}, column {exc.colno}: {exc.msg}") from exc
    if not isinstance(raw, dict):
        raise RulesProfileError("Rules profile JSON must be an object.")
    return _profile_from_mapping(raw, source=str(path.resolve()))


def rules_profile_from_mapping(raw: dict[str, Any], *, source: str = "inline") -> RulesProfile:
    if not isinstance(raw, dict):
        raise RulesProfileError("Rules profile JSON must be an object.")
    return _profile_from_mapping(raw, source=source)


def _profile_from_mapping(raw: dict[str, Any], *, source: str) -> RulesProfile:
    profile = default_rules_profile()
    quality = _optional_object(raw, "quality_thresholds")
    rules = _optional_object(raw, "rules")
    template = _optional_object(raw, "template")
    template_id = _optional_rule_template_id(template, "id", profile.template_id)
    return RulesProfile(
        name=_optional_string(raw, "name", profile.name),
        version=_optional_string(raw, "version", profile.version),
        source=source,
        template_id=template_id,
        template_version=_optional_nullable_string(template, "version", profile.template_version),
        template_source=_optional_nullable_string(template, "source", profile.template_source),
        min_dpi=_optional_int(raw, "min_dpi", profile.min_dpi),
        dpi_purpose=_optional_dpi_purpose(raw, "dpi_purpose", profile.dpi_purpose),
        name_pattern=_optional_nullable_string(raw, "name_pattern", profile.name_pattern),
        dark_mean_threshold=_optional_float(quality, "dark_mean_threshold", profile.dark_mean_threshold),
        bright_mean_threshold=_optional_float(quality, "bright_mean_threshold", profile.bright_mean_threshold),
        low_contrast_stddev_threshold=_optional_float(
            quality,
            "low_contrast_stddev_threshold",
            profile.low_contrast_stddev_threshold,
        ),
        blur_laplacian_variance_threshold=_optional_float(
            quality,
            "blur_laplacian_variance_threshold",
            profile.blur_laplacian_variance_threshold,
        ),
        blur_min_contrast_stddev=_optional_float(
            quality,
            "blur_min_contrast_stddev",
            profile.blur_min_contrast_stddev,
        ),
        blank_brightness_min=_optional_float(quality, "blank_brightness_min", profile.blank_brightness_min),
        blank_contrast_max=_optional_float(quality, "blank_contrast_max", profile.blank_contrast_max),
        blank_foreground_coverage_max=_optional_float(
            quality,
            "blank_foreground_coverage_max",
            profile.blank_foreground_coverage_max,
        ),
        blank_edge_coverage_max=_optional_float(quality, "blank_edge_coverage_max", profile.blank_edge_coverage_max),
        blank_dark_pixel_ratio_max=_optional_float(
            quality,
            "blank_dark_pixel_ratio_max",
            profile.blank_dark_pixel_ratio_max,
        ),
        despeckle_max_pixel_change_ratio=_optional_float(
            quality,
            "despeckle_max_pixel_change_ratio",
            profile.despeckle_max_pixel_change_ratio,
        ),
        deskew_residual_threshold=_optional_float(
            quality,
            "deskew_residual_threshold",
            profile.deskew_residual_threshold,
        ),
        rules=_rules_from_mapping(rules),
    )


def _optional_object(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key, {})
    if not isinstance(value, dict):
        raise RulesProfileError(f"Rules profile field '{key}' must be an object.")
    return value


def _optional_dpi_purpose(raw: dict[str, Any], key: str, default: str) -> str:
    value = raw.get(key, default)
    if not isinstance(value, str) or value not in VALID_DPI_PURPOSES:
        raise RulesProfileError(
            f"Rules profile field '{key}' must be one of {', '.join(sorted(VALID_DPI_PURPOSES))}."
        )
    return value


def _optional_string(raw: dict[str, Any], key: str, default: str) -> str:
    value = raw.get(key, default)
    if not isinstance(value, str) or not value:
        raise RulesProfileError(f"Rules profile field '{key}' must be a non-empty string.")
    return value


def _optional_nullable_string(raw: dict[str, Any], key: str, default: str | None) -> str | None:
    value = raw.get(key, default)
    if value is None or isinstance(value, str):
        return value
    raise RulesProfileError(f"Rules profile field '{key}' must be a string or null.")


def _optional_rule_template_id(raw: dict[str, Any], key: str, default: str | None) -> str | None:
    value = _optional_nullable_string(raw, key, default)
    if value is not None and value not in RULE_TEMPLATE_IDS:
        raise RulesProfileError(
            f"Rules profile field 'template.{key}' must be one of {', '.join(RULE_TEMPLATE_IDS)}."
        )
    return value


def _optional_int(raw: dict[str, Any], key: str, default: int) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise RulesProfileError(f"Rules profile field '{key}' must be an integer.")
    if value < 0:
        raise RulesProfileError(f"Rules profile field '{key}' must be greater than or equal to 0.")
    return value


def _optional_float(raw: dict[str, Any], key: str, default: float) -> float:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RulesProfileError(f"Rules profile field 'quality_thresholds.{key}' must be a number.")
    return float(value)


def _rules_from_mapping(raw: dict[str, Any]) -> dict[str, RuleSetting]:
    settings: dict[str, RuleSetting] = {}
    for rule, value in raw.items():
        if not isinstance(rule, str) or not rule:
            raise RulesProfileError("Rules profile rule names must be non-empty strings.")
        if not isinstance(value, dict):
            raise RulesProfileError(f"Rules profile field 'rules.{rule}' must be an object.")
        enabled = value.get("enabled", True)
        if not isinstance(enabled, bool):
            raise RulesProfileError(f"Rules profile field 'rules.{rule}.enabled' must be a boolean.")
        severity = value.get("severity")
        if severity is not None and severity not in VALID_SEVERITIES:
            raise RulesProfileError(
                f"Rules profile field 'rules.{rule}.severity' must be one of P0, P1, or P2."
            )
        settings[rule] = RuleSetting(enabled=enabled, severity=severity)
    return settings
