#!/usr/bin/env python3
"""Run semantic CI regression groups for ai4archive."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile


REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = REPO_ROOT / "tests"

CORE_IMAGE_PROCESSING_TESTS = (
    "test_ai4_863_optimizations",
    "test_ai4_864_deskew_vectorization",
    "test_ai4_866_despeckle_fallback_parity",
    "test_ai4_867_numpy_backend",
    "test_ai4_869_numpy_despeckle_filtering",
    "test_backend_consistency",
    "test_content_type_regression",
    "test_dat_10_2_deskew_post_verification",
    "test_dat_10_3_crop_margin",
    "test_dat_10_4_despeckle_preservation",
    "test_deskew_optimization",
    "test_despeckle_opencv_backend",
    "test_image_io_vips_backend",
    "test_image_processing_capability_smoke",
    "test_performance_suite",
    "test_quality_suite",
    "test_scan_background_stains",
    "test_scan_edge_shadow",
    "test_scan_processing_algorithm_regression",
    "test_scan_processing_combo",
    "test_scan_processing_reuse",
    "test_scan_processing_workflow_regression",
    "test_scan_qc",
    "test_scan_tone_normalization",
    "test_scanline_lightening",
    "test_worker_recommendation",
)

PRODUCTION_CLI_TESTS = (
    "test_acceptance",
    "test_cli_smoke",
    "test_cli_stable_contract",
    "test_dat_9_4_tiered_resolution",
    "test_dat_12_3_acceptance_verdict",
    "test_dat_12_3_sampling_loop",
    "test_local_workbench_autosave",
    "test_manifest",
    "test_preflight_run_plan",
    "test_processing_review",
    "test_production_rehearsal",
    "test_production_review_queue",
    "test_production_workbench_completion_handoff",
    "test_production_workbench_regression_guards",
    "test_reports_contract",
    "test_rule_registry",
    "test_rules",
    "test_sampling",
    "test_service_api",
    "test_service_jobs",
)

PRIVACY_BOUNDARY_TESTS = (
    "test_acceptance_summary_regression",
    "test_aggregate_baseline_regression",
    "test_analysis_provider",
    "test_artifact_readiness",
    "test_capability_probe",
    "test_deep_inspection_candidates",
    "test_deep_inspection_provider",
    "test_evidence_bundle",
    "test_final_handoff",
    "test_handoff_manifest",
    "test_public_capability_contract",
    "test_release_summaries",
    "test_review_decisions",
    "test_rework_actions",
    "test_rules_calibration",
    "test_validation_index",
    "test_workbench_summary",
)

EXTERNAL_VALIDATION_TESTS = (
    "test_ci_regression_groups",
    "test_ci_targeted_selector",
    "test_delivery_tooling",
)

REGRESSION_GROUPS = {
    "core-image-processing": CORE_IMAGE_PROCESSING_TESTS,
    "production-cli": PRODUCTION_CLI_TESTS,
    "privacy-boundary": PRIVACY_BOUNDARY_TESTS,
    "external-validation": EXTERNAL_VALIDATION_TESTS,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list-groups", help="Print group names.")
    list_tests = subparsers.add_parser("list-tests", help="Print unittest modules for a group.")
    list_tests.add_argument("group", choices=sorted(REGRESSION_GROUPS))
    run = subparsers.add_parser("run", help="Run one regression group.")
    run.add_argument("group", choices=sorted(REGRESSION_GROUPS))
    subparsers.add_parser("verify-coverage", help="Fail if any test module is ungrouped or duplicated.")
    args = parser.parse_args(argv)

    if args.command == "list-groups":
        for group in sorted(REGRESSION_GROUPS):
            print(group)
        return 0
    if args.command == "list-tests":
        for test in REGRESSION_GROUPS[args.group]:
            print(test)
        return 0
    if args.command == "verify-coverage":
        verify_group_coverage()
        return 0
    if args.command == "run":
        verify_group_coverage()
        run_group(args.group)
        return 0
    raise AssertionError(f"Unhandled command: {args.command}")


def verify_group_coverage() -> None:
    existing = {path.stem for path in TESTS_DIR.glob("test_*.py")}
    assigned: dict[str, list[str]] = {}
    for group, tests in REGRESSION_GROUPS.items():
        for test in tests:
            assigned.setdefault(test, []).append(group)

    missing = sorted(existing - set(assigned))
    stale = sorted(set(assigned) - existing)
    duplicates = {test: groups for test, groups in sorted(assigned.items()) if len(groups) > 1}
    if missing or stale or duplicates:
        lines = ["CI regression group coverage is not exact."]
        if missing:
            lines.append("Ungrouped tests: " + ", ".join(missing))
        if stale:
            lines.append("Grouped tests missing on disk: " + ", ".join(stale))
        if duplicates:
            lines.append(
                "Tests assigned to multiple groups: "
                + ", ".join(f"{test}={','.join(groups)}" for test, groups in duplicates.items())
            )
        raise SystemExit("\n".join(lines))


def run_group(group: str) -> None:
    tests = REGRESSION_GROUPS[group]
    print(f"Running regression group: {group}", flush=True)
    print("Unit test modules: " + " ".join(tests), flush=True)
    run_command([sys.executable, "-m", "unittest", *tests])
    if group == "external-validation":
        run_external_validation_commands()


def run_external_validation_commands() -> None:
    with tempfile.TemporaryDirectory(prefix="ai4-ci-external-validation-") as temp_dir:
        root = Path(temp_dir)
        commands = (
            [
                sys.executable,
                "-m",
                "archive_scan_qc",
                "image-processing-capability-smoke",
                "--out",
                str(root / "image-processing-capability-smoke"),
                "--workers",
                "1",
            ],
            [
                sys.executable,
                "-m",
                "archive_scan_qc",
                "public-capability-contract",
                "--out",
                str(root / "public-capability-contract"),
            ],
            [
                sys.executable,
                "-m",
                "archive_scan_qc",
                "capability-probe",
                "--out",
                str(root / "capability-probe"),
                "--no-torch-cuda-check",
            ],
            [
                sys.executable,
                str(REPO_ROOT / "scripts" / "run_dibco_external_cli_test.py"),
                "--synthetic-smoke",
                "--data-root",
                str(root / "dibco-data"),
                "--output-root",
                str(root / "dibco-output"),
                "--run-id",
                "ci-smoke",
                "--workers",
                "1",
                "--no-doc-report",
            ],
            [
                sys.executable,
                str(REPO_ROOT / "scripts" / "run_noisyoffice_external_cli_test.py"),
                "--synthetic-smoke",
                "--data-root",
                str(root / "noisyoffice-data"),
                "--output-root",
                str(root / "noisyoffice-output"),
                "--run-id",
                "ci-smoke",
                "--workers",
                "1",
                "--no-download",
                "--no-doc-report",
            ],
        )
        for command in commands:
            run_command(command)


def run_command(command: list[str]) -> None:
    env = os.environ.copy()
    pythonpath_parts = [str(REPO_ROOT / "src"), str(REPO_ROOT / "tests")]
    if env.get("PYTHONPATH"):
        pythonpath_parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
    print("+ " + " ".join(command), flush=True)
    subprocess.run(command, cwd=REPO_ROOT, env=env, check=True)


if __name__ == "__main__":
    raise SystemExit(main())
