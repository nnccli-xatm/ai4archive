"""Rules profile loading and validation for scan QC."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any


VALID_SEVERITIES = {"P0", "P1", "P2"}


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
    min_dpi: int = 200
    name_pattern: str | None = None
    dark_mean_threshold: float = 45.0
    bright_mean_threshold: float = 250.0
    low_contrast_stddev_threshold: float = 10.0
    blur_laplacian_variance_threshold: float = 20.0
    blur_min_contrast_stddev: float = 12.0
    rules: dict[str, RuleSetting] = field(default_factory=dict)

    def is_rule_enabled(self, rule: str) -> bool:
        return self.rules.get(rule, RuleSetting()).enabled

    def severity_for(self, rule: str, default: str) -> str:
        return self.rules.get(rule, RuleSetting()).severity or default

    def threshold_summary(self) -> dict[str, Any]:
        return {
            "min_dpi": self.min_dpi,
            "name_pattern": self.name_pattern,
            "quality": {
                "dark_mean_threshold": self.dark_mean_threshold,
                "bright_mean_threshold": self.bright_mean_threshold,
                "low_contrast_stddev_threshold": self.low_contrast_stddev_threshold,
                "blur_laplacian_variance_threshold": self.blur_laplacian_variance_threshold,
                "blur_min_contrast_stddev": self.blur_min_contrast_stddev,
            },
        }

    def metadata(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "source": self.source,
            "thresholds": self.threshold_summary(),
            "rules": {
                rule: {"enabled": setting.enabled, "severity": setting.severity}
                for rule, setting in sorted(self.rules.items())
            },
        }


def default_rules_profile() -> RulesProfile:
    return RulesProfile()


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


def _profile_from_mapping(raw: dict[str, Any], *, source: str) -> RulesProfile:
    profile = default_rules_profile()
    quality = _optional_object(raw, "quality_thresholds")
    rules = _optional_object(raw, "rules")
    return RulesProfile(
        name=_optional_string(raw, "name", profile.name),
        version=_optional_string(raw, "version", profile.version),
        source=source,
        min_dpi=_optional_int(raw, "min_dpi", profile.min_dpi),
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
        rules=_rules_from_mapping(rules),
    )


def _optional_object(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key, {})
    if not isinstance(value, dict):
        raise RulesProfileError(f"Rules profile field '{key}' must be an object.")
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
