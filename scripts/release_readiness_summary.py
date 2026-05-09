#!/usr/bin/env python3
"""Write aggregate-only production release readiness evidence."""

from __future__ import annotations

import argparse
import compileall
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Callable


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
RELEASE_READINESS_JSON = "release_readiness_summary.json"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from archive_scan_qc.capability_probe import CapabilityProbeConfig, run_capability_probe  # noqa: E402


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class ReadinessCheck:
    name: str
    status: str
    blocking: bool
    evidence_count: int = 1

    def as_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "blocking": self.blocking,
            "evidence_count": self.evidence_count,
        }


def build_release_readiness_summary(
    *,
    checks: list[ReadinessCheck],
    capability_probe: dict[str, Any] | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    blocking_count = sum(1 for check in checks if check.blocking)
    failed_count = sum(1 for check in checks if check.status == "fail")
    skipped_count = sum(1 for check in checks if check.status == "skipped")
    warning_count = sum(1 for check in checks if check.status == "warning")
    passed_count = sum(1 for check in checks if check.status == "pass")

    probe_summary = _capability_probe_summary(capability_probe)
    if probe_summary["available"] and probe_summary["blocking"]:
        blocking_count += 1
        failed_count += 1

    status = "pass" if blocking_count == 0 else "fail"
    return {
        "schema_version": "scan-qc.release-readiness.v1",
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "status": status,
        "privacy": {
            "aggregate_only": True,
            "contains_paths": False,
            "contains_filenames": False,
            "contains_hashes": False,
            "contains_ocr_text": False,
            "contains_thumbnails": False,
            "contains_image_content": False,
            "contains_secrets": False,
            "contains_row_level_findings": False,
        },
        "runtime": {
            "python_version_family": f"{sys.version_info.major}.{sys.version_info.minor}",
        },
        "summary": {
            "checks_total": len(checks),
            "checks_passed": passed_count,
            "checks_failed": failed_count,
            "checks_warning": warning_count,
            "checks_skipped": skipped_count,
            "blocking_items": blocking_count,
        },
        "checks": {check.name: check.as_json() for check in checks},
        "capability_probe": probe_summary,
        "sensitive_artifacts": {
            "local_only": True,
            "paths_embedded": False,
            "operator_note": "Review local validation outputs on the validation host; this summary intentionally omits paths and row-level artifacts.",
        },
        "scan_processing_semantics": "unchanged_cpu_pillow_baseline",
        "network_services_called": False,
        "model_inference_run": False,
    }


def run_release_readiness_checks(
    *,
    run_unit_tests: bool = True,
    run_compile: bool = True,
    run_offline_dependencies: bool = True,
    run_cli_smoke: bool = True,
    wheelhouse_path: Path | None = None,
    capability_probe_path: Path | None = None,
    run_capability_probe_check: bool = False,
    command_runner: CommandRunner | None = None,
) -> dict[str, Any]:
    command_runner = command_runner or subprocess.run
    env = _pythonpath_env()
    checks: list[ReadinessCheck] = []

    if run_unit_tests:
        checks.append(
            _command_check(
                "unit_tests",
                [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                command_runner=command_runner,
                env=env,
            )
        )

    if run_compile:
        checks.append(_compile_check())

    if run_offline_dependencies:
        command = [sys.executable, str(REPO_ROOT / "scripts" / "check_offline_dependencies.py")]
        if wheelhouse_path is not None:
            command.extend(["--wheelhouse", str(wheelhouse_path)])
        checks.append(
            _command_check(
                "offline_dependency_check",
                command,
                command_runner=command_runner,
                env=env,
            )
        )

    if run_cli_smoke:
        checks.append(
            _command_check(
                "package_cli_smoke",
                [sys.executable, "-m", "archive_scan_qc", "--version"],
                command_runner=command_runner,
                env=env,
            )
        )

    capability_probe = _load_capability_probe(capability_probe_path)
    if capability_probe is None and run_capability_probe_check:
        capability_probe = run_capability_probe(CapabilityProbeConfig(include_torch_cuda=False))

    return build_release_readiness_summary(checks=checks, capability_probe=capability_probe)


def write_release_readiness_summary(summary: dict[str, Any], output_path: Path) -> Path:
    path = output_path / RELEASE_READINESS_JSON if output_path.suffix == "" else output_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _pythonpath_env() -> dict[str, str]:
    env = os.environ.copy()
    src = str(SRC_DIR)
    env["PYTHONPATH"] = src if not env.get("PYTHONPATH") else src + os.pathsep + env["PYTHONPATH"]
    return env


def _command_check(
    name: str,
    command: list[str],
    *,
    command_runner: CommandRunner,
    env: dict[str, str],
) -> ReadinessCheck:
    result = command_runner(command, cwd=REPO_ROOT, env=env, check=False, capture_output=True, text=True)
    status = "pass" if result.returncode == 0 else "fail"
    return ReadinessCheck(name=name, status=status, blocking=status == "fail")


def _compile_check() -> ReadinessCheck:
    ok = compileall.compile_dir(SRC_DIR, quiet=1) and compileall.compile_dir(REPO_ROOT / "tests", quiet=1)
    return ReadinessCheck(name="compile_import_check", status="pass" if ok else "fail", blocking=not ok)


def _load_capability_probe(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _capability_probe_summary(report: dict[str, Any] | None) -> dict[str, Any]:
    if report is None:
        return {
            "available": False,
            "status": "skipped",
            "blocking": False,
            "provider_packages_found_count": 0,
            "gpu_visible_count": 0,
            "gpu_acceleration_configured": False,
            "model_acceleration_configured": False,
        }

    readiness = report.get("readiness", {})
    visibility = report.get("gpu_provider_visibility", {})
    provider_packages = readiness.get("provider_packages_found", [])
    blocking = bool(readiness.get("blocking", False)) or report.get("status") == "fail"
    return {
        "available": True,
        "status": "fail" if blocking else "pass",
        "blocking": blocking,
        "provider_packages_found_count": len(provider_packages) if isinstance(provider_packages, list) else 0,
        "gpu_visible_count": int(visibility.get("gpu_visible_count") or 0),
        "gpu_acceleration_configured": bool(readiness.get("gpu_acceleration_configured", False)),
        "model_acceleration_configured": bool(readiness.get("model_acceleration_configured", False)),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Write aggregate-only production release readiness evidence without network or model inference."
    )
    parser.add_argument("--out", type=Path, default=Path(RELEASE_READINESS_JSON), help="Output JSON path or directory.")
    parser.add_argument("--wheelhouse", type=Path, default=None, help="Optional local wheelhouse directory to verify.")
    parser.add_argument("--capability-probe", type=Path, default=None, help="Optional existing capability_probe.json to summarize.")
    parser.add_argument(
        "--run-capability-probe",
        action="store_true",
        help="Run the local aggregate capability probe without torch CUDA import or inference.",
    )
    parser.add_argument("--skip-unit-tests", action="store_true", help="Skip unittest readiness evidence.")
    parser.add_argument("--skip-compile", action="store_true", help="Skip compile/import readiness evidence.")
    parser.add_argument("--skip-offline-dependencies", action="store_true", help="Skip offline dependency readiness evidence.")
    parser.add_argument("--skip-cli-smoke", action="store_true", help="Skip package/CLI smoke readiness evidence.")
    args = parser.parse_args(argv)

    summary = run_release_readiness_checks(
        run_unit_tests=not args.skip_unit_tests,
        run_compile=not args.skip_compile,
        run_offline_dependencies=not args.skip_offline_dependencies,
        run_cli_smoke=not args.skip_cli_smoke,
        wheelhouse_path=args.wheelhouse,
        capability_probe_path=args.capability_probe,
        run_capability_probe_check=args.run_capability_probe,
    )
    path = write_release_readiness_summary(summary, args.out)
    print(f"Release readiness summary: {path}")
    print(f"Release readiness status: {summary['status']}")
    print(f"Blocking items: {summary['summary']['blocking_items']}")
    print("Privacy: aggregate-only; no paths, filenames, hashes, OCR text, thumbnails, image content, secrets, or row-level findings.")
    return 0 if summary["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
