"""Privacy-safe deep-inspection provider configuration scaffold.

This module intentionally validates provider configuration metadata only. It
does not execute provider commands, open images, or accept row-level evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any


DEEP_INSPECTION_PROBE_JSON = "deep_inspection_provider_probe.json"
DEEP_INSPECTION_SCHEMA_VERSION = "scan-qc.deep-inspection-provider.v1"
PRIVATE_FIELD_TOKENS = (
    "file",
    "filename",
    "hash",
    "image",
    "ocr",
    "path",
    "sha",
    "text",
    "thumbnail",
)
PRIVATE_VALUE_TOKENS = ("/", "\\", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", "data:image")


class DeepInspectionProviderConfigError(ValueError):
    """Raised when provider scaffold configuration is invalid or unsafe."""


@dataclass(frozen=True)
class DeepInspectionProvider:
    name: str
    command: str | None = None
    config: dict[str, Any] = field(default_factory=dict)
    requirements: tuple[str, ...] = ()


@dataclass(frozen=True)
class DeepInspectionProviderConfig:
    enabled: bool = False
    providers: tuple[DeepInspectionProvider, ...] = ()


def load_deep_inspection_provider_config(path: Path | None) -> DeepInspectionProviderConfig:
    if path is None:
        return DeepInspectionProviderConfig()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise DeepInspectionProviderConfigError("Deep-inspection provider config must be a JSON object.")
    return parse_deep_inspection_provider_config(payload)


def parse_deep_inspection_provider_config(payload: dict[str, Any]) -> DeepInspectionProviderConfig:
    enabled = bool(payload.get("enabled", False))
    raw_providers = payload.get("providers", [])
    if not isinstance(raw_providers, list):
        raise DeepInspectionProviderConfigError("Deep-inspection provider config field 'providers' must be a list.")

    providers = tuple(_parse_provider(item, index) for index, item in enumerate(raw_providers, start=1))
    if not enabled and providers:
        raise DeepInspectionProviderConfigError("Deep-inspection providers must not be configured while enabled=false.")
    return DeepInspectionProviderConfig(enabled=enabled, providers=providers)


def build_deep_inspection_provider_probe(config: DeepInspectionProviderConfig | None = None) -> dict[str, Any]:
    config = config or DeepInspectionProviderConfig()
    configured = config.enabled and bool(config.providers)
    missing_requirements = sorted(
        {
            requirement
            for provider in config.providers
            for requirement in provider.requirements
            if isinstance(requirement, str) and requirement.strip()
        }
    )
    provider_names = sorted(provider.name for provider in config.providers)
    return {
        "schema_version": DEEP_INSPECTION_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "pass",
        "configured": configured,
        "provider_count": len(config.providers) if config.enabled else 0,
        "provider_names": provider_names if config.enabled else [],
        "missing_requirements": missing_requirements if configured else [],
        "no_inference_run": True,
        "scan_processing_semantics": "unchanged_cpu_pillow_baseline",
        "privacy": {
            "aggregate_only": True,
            "contains_paths": False,
            "contains_filenames": False,
            "contains_hashes": False,
            "contains_thumbnails": False,
            "contains_ocr_text": False,
            "contains_image_content": False,
            "contains_row_level_evidence": False,
            "network_calls": False,
        },
    }


def write_deep_inspection_provider_probe(report: dict[str, Any], output_path: Path) -> Path:
    path = output_path / DEEP_INSPECTION_PROBE_JSON if output_path.is_dir() or output_path.suffix == "" else output_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _parse_provider(payload: Any, index: int) -> DeepInspectionProvider:
    if not isinstance(payload, dict):
        raise DeepInspectionProviderConfigError(f"Deep-inspection provider #{index} must be a JSON object.")
    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        raise DeepInspectionProviderConfigError(f"Deep-inspection provider #{index} field 'name' must be a non-empty string.")
    _reject_private_metadata(payload, f"providers[{index - 1}]")
    command = payload.get("command")
    if command is not None and (not isinstance(command, str) or not command.strip()):
        raise DeepInspectionProviderConfigError(f"Deep-inspection provider '{name}' field 'command' must be a non-empty string.")
    config = payload.get("config", {})
    if not isinstance(config, dict):
        raise DeepInspectionProviderConfigError(f"Deep-inspection provider '{name}' field 'config' must be an object.")
    requirements = payload.get("requirements", [])
    if not isinstance(requirements, list) or not all(isinstance(item, str) for item in requirements):
        raise DeepInspectionProviderConfigError(f"Deep-inspection provider '{name}' field 'requirements' must be a list of strings.")
    return DeepInspectionProvider(
        name=name.strip(),
        command=command.strip() if isinstance(command, str) else None,
        config=_safe_config(config),
        requirements=tuple(item.strip() for item in requirements if item.strip()),
    )


def _safe_config(payload: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in payload.items():
        if not isinstance(key, str) or _is_private_key(key):
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            if isinstance(value, str) and _looks_private_value(value):
                continue
            safe[key] = value
        elif isinstance(value, list):
            safe[key] = [
                item
                for item in value
                if (isinstance(item, (int, float, bool)) or item is None)
                or (isinstance(item, str) and not _looks_private_value(item))
            ]
        elif isinstance(value, dict):
            safe[key] = _safe_config(value)
    return safe


def _reject_private_metadata(value: Any, location: str) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and _is_private_key(key):
                raise DeepInspectionProviderConfigError(f"Private provider field is not allowed at {location}.{key}.")
            _reject_private_metadata(item, f"{location}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_private_metadata(item, f"{location}[{index}]")
    elif isinstance(value, str) and _looks_private_value(value):
        raise DeepInspectionProviderConfigError(f"Private provider value is not allowed at {location}.")


def _is_private_key(key: str) -> bool:
    lowered = key.lower()
    parts = [part for part in re.split(r"[^a-z0-9]+", lowered) if part]
    return any(part == token or part.startswith(token) for part in parts for token in PRIVATE_FIELD_TOKENS)


def _looks_private_value(value: str) -> bool:
    lowered = value.lower()
    return any(token in lowered for token in PRIVATE_VALUE_TOKENS)
