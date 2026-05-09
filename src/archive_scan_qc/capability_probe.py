"""Privacy-safe local capability probe for optional model/GPU providers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import importlib.util
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Callable


CAPABILITY_PROBE_JSON = "capability_probe.json"
OPTIONAL_PACKAGES = {
    "onnxruntime": "onnxruntime",
    "paddle": "paddle",
    "paddleocr": "paddleocr",
    "torch": "torch",
    "opencv": "cv2",
    "cupy": "cupy",
}
GPU_ACCELERATION_ENV_FLAGS = ("ARCHIVE_SCAN_QC_GPU_ACCELERATION_ENABLED", "SCAN_QC_GPU_ACCELERATION_ENABLED")
MODEL_ACCELERATION_ENV_FLAGS = ("ARCHIVE_SCAN_QC_MODEL_ACCELERATION_ENABLED", "SCAN_QC_MODEL_ACCELERATION_ENABLED")
ANALYSIS_PROVIDER_ENV_FLAGS = ("ARCHIVE_SCAN_QC_ANALYSIS_PROVIDER_COMMAND", "SCAN_QC_ANALYSIS_PROVIDER_COMMAND")


@dataclass(frozen=True)
class CapabilityProbeConfig:
    analysis_provider_command: str | None = None
    gpu_acceleration_enabled: bool | None = None
    model_acceleration_enabled: bool | None = None
    include_torch_cuda: bool = True


def run_capability_probe(
    config: CapabilityProbeConfig | None = None,
    *,
    package_available: Callable[[str], bool] | None = None,
    command_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    environ: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Return aggregate readiness for optional local providers without inference."""

    config = config or CapabilityProbeConfig()
    package_available = package_available or _package_available
    command_runner = command_runner or subprocess.run
    environ = os.environ if environ is None else environ

    packages = _optional_package_summary(package_available)
    nvidia = _nvidia_gpu_summary(command_runner)
    torch_cuda = _torch_cuda_summary(packages["torch"]["available"], config.include_torch_cuda)
    configured = _configured_summary(config, environ)
    provider_packages_found = sorted(name for name, item in packages.items() if item["available"])
    gpu_visible_count = max(int(nvidia["visible_count"]), int(torch_cuda["visible_count"] or 0))
    provider_ready = bool(provider_packages_found or gpu_visible_count)

    return {
        "schema_version": "scan-qc.capability-probe.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "pass",
        "privacy": {
            "aggregate_only": True,
            "contains_file_list": False,
            "contains_paths": False,
            "contains_hashes": False,
            "contains_thumbnails": False,
            "contains_ocr_text": False,
            "contains_image_content": False,
            "contains_environment_values": False,
        },
        "optional_packages": packages,
        "gpu_provider_visibility": {
            "nvidia_smi": nvidia,
            "torch_cuda": torch_cuda,
            "gpu_visible_count": gpu_visible_count,
        },
        "configuration": configured,
        "readiness": {
            "provider_packages_found": provider_packages_found,
            "gpu_or_model_provider_visible": provider_ready,
            "gpu_acceleration_configured": configured["gpu_acceleration_configured"],
            "model_acceleration_configured": configured["model_acceleration_configured"],
            "scan_processing_semantics": "unchanged_cpu_pillow_baseline",
            "blocking": False,
        },
        "warnings": _warnings(packages, nvidia, torch_cuda),
    }


def write_capability_probe(report: dict[str, Any], output_path: Path) -> Path:
    path = output_path / CAPABILITY_PROBE_JSON if output_path.suffix == "" else output_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _optional_package_summary(package_available: Callable[[str], bool]) -> dict[str, dict[str, Any]]:
    return {
        label: {
            "module": module,
            "available": bool(package_available(module)),
            "required": False,
        }
        for label, module in OPTIONAL_PACKAGES.items()
    }


def _package_available(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


def _nvidia_gpu_summary(command_runner: Callable[..., subprocess.CompletedProcess[str]]) -> dict[str, Any]:
    try:
        result = command_runner(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except FileNotFoundError:
        return {"available": False, "visible_count": 0, "memory_total_gb": 0.0, "status": "unavailable"}
    except (OSError, subprocess.TimeoutExpired):
        return {"available": False, "visible_count": 0, "memory_total_gb": 0.0, "status": "probe_failed"}

    if result.returncode != 0:
        return {"available": False, "visible_count": 0, "memory_total_gb": 0.0, "status": "no_usable_telemetry"}

    memory_mib: list[float] = []
    for line in result.stdout.splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            memory_mib.append(float(text))
        except ValueError:
            return {"available": False, "visible_count": 0, "memory_total_gb": 0.0, "status": "unparseable"}

    return {
        "available": bool(memory_mib),
        "visible_count": len(memory_mib),
        "memory_total_gb": round(sum(memory_mib) / 1024, 3),
        "status": "visible" if memory_mib else "no_usable_telemetry",
    }


def _torch_cuda_summary(torch_available: bool, include_torch_cuda: bool) -> dict[str, Any]:
    if not torch_available or not include_torch_cuda:
        return {"checked": bool(include_torch_cuda), "available": False, "visible_count": None, "status": "not_checked"}
    try:
        import torch  # type: ignore[import-not-found]

        available = bool(torch.cuda.is_available())
        count = int(torch.cuda.device_count()) if available else 0
    except Exception:
        return {"checked": True, "available": False, "visible_count": 0, "status": "probe_failed"}
    return {"checked": True, "available": available, "visible_count": count, "status": "visible" if available else "unavailable"}


def _configured_summary(config: CapabilityProbeConfig, environ: dict[str, str]) -> dict[str, Any]:
    analysis_provider_configured = bool(config.analysis_provider_command) or any(_env_has_value(environ, name) for name in ANALYSIS_PROVIDER_ENV_FLAGS)
    gpu_configured = _explicit_or_env_flag(config.gpu_acceleration_enabled, environ, GPU_ACCELERATION_ENV_FLAGS)
    model_configured = (
        _explicit_or_env_flag(config.model_acceleration_enabled, environ, MODEL_ACCELERATION_ENV_FLAGS)
        or analysis_provider_configured
    )
    return {
        "analysis_provider_configured": analysis_provider_configured,
        "gpu_acceleration_configured": gpu_configured,
        "model_acceleration_configured": model_configured,
        "configuration_sources": {
            "cli_analysis_provider_command": bool(config.analysis_provider_command),
            "analysis_provider_env_present": any(_env_has_value(environ, name) for name in ANALYSIS_PROVIDER_ENV_FLAGS),
            "gpu_env_flag_present": any(_env_has_value(environ, name) for name in GPU_ACCELERATION_ENV_FLAGS),
            "model_env_flag_present": any(_env_has_value(environ, name) for name in MODEL_ACCELERATION_ENV_FLAGS),
        },
    }


def _explicit_or_env_flag(value: bool | None, environ: dict[str, str], names: tuple[str, ...]) -> bool:
    if value is not None:
        return bool(value)
    return any(_env_enabled(environ, name) for name in names)


def _env_has_value(environ: dict[str, str], name: str) -> bool:
    return bool(environ.get(name, "").strip())


def _env_enabled(environ: dict[str, str], name: str) -> bool:
    return environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _warnings(packages: dict[str, dict[str, Any]], nvidia: dict[str, Any], torch_cuda: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    if not any(item["available"] for item in packages.values()):
        warnings.append("No optional local model provider packages were found; CPU/Pillow baseline remains available.")
    if not nvidia["available"] and not torch_cuda["available"]:
        warnings.append("No GPU provider visibility was confirmed; GPU/model acceleration readiness is informational only.")
    return warnings
