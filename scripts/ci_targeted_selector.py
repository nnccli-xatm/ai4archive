#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


@dataclass(frozen=True)
class MethodSpan:
    class_name: str
    method_name: str
    start: int
    end: int

    @property
    def test_id(self) -> str:
        return f"{self.class_name}.{self.method_name}"


FAST_CONTRACT_TESTS = {
    "tests/test_acceptance.py",
    "tests/test_manifest.py",
    "tests/test_rule_registry.py",
    "tests/test_rules.py",
    "tests/test_sampling.py",
}


PROCESSING_TARGETED_TESTS = {
    "tests/test_backend_consistency.py",
    "tests/test_content_type_regression.py",
    "tests/test_dat_10_2_deskew_post_verification.py",
    "tests/test_dat_10_3_crop_margin.py",
    "tests/test_dat_10_4_despeckle_preservation.py",
    "tests/test_deskew_optimization.py",
    "tests/test_despeckle_opencv_backend.py",
    "tests/test_image_io_vips_backend.py",
    "tests/test_image_processing_capability_smoke.py",
    "tests/test_quality_suite.py",
    "tests/test_scan_background_stains.py",
    "tests/test_scan_edge_shadow.py",
    "tests/test_scan_processing_combo.py",
    "tests/test_scan_processing_reuse.py",
    "tests/test_scan_processing_workflow_regression.py",
    "tests/test_scan_tone_normalization.py",
    "tests/test_scanline_lightening.py",
}


SOURCE_TEST_MAP: dict[str, set[str]] = {
    "__main__.py": {"tests/test_cli_smoke.py"},
    "acceptance.py": {
        "tests/test_acceptance.py",
        "tests/test_acceptance_summary_regression.py",
        "tests/test_dat_12_3_acceptance_verdict.py",
    },
    "analysis_provider.py": {"tests/test_analysis_provider.py"},
    "artifact_readiness.py": {"tests/test_artifact_readiness.py"},
    "benchmark.py": {"tests/test_performance_suite.py", "tests/test_worker_recommendation.py"},
    "calibration.py": {"tests/test_rules_calibration.py"},
    "capability_probe.py": {"tests/test_capability_probe.py"},
    "cli.py": {
        "tests/test_cli_smoke.py",
        "tests/test_cli_stable_contract.py",
        "tests/test_image_processing_capability_smoke.py",
        "tests/test_public_capability_contract.py",
        "tests/test_production_review_queue.py",
        "tests/test_scan_background_stains.py",
        "tests/test_scan_processing_combo.py",
        "tests/test_scan_processing_reuse.py",
        "tests/test_scan_tone_normalization.py",
        "tests/test_scanline_lightening.py",
        "tests/test_service_http.py",
    },
    "concurrency.py": {"tests/test_worker_recommendation.py"},
    "deep_inspection_candidates.py": {"tests/test_deep_inspection_candidates.py"},
    "deep_inspection_provider.py": {"tests/test_deep_inspection_provider.py"},
    "evidence_bundle.py": {"tests/test_evidence_bundle.py"},
    "final_handoff.py": {"tests/test_final_handoff.py"},
    "handoff.py": {"tests/test_handoff_manifest.py"},
    "image_processing_capability_smoke.py": {"tests/test_image_processing_capability_smoke.py"},
    "local_workbench.py": {
        "tests/test_local_workbench_autosave.py",
        "tests/test_production_workbench_completion_handoff.py",
        "tests/test_production_workbench_regression_guards.py",
    },
    "manifest.py": {"tests/test_manifest.py"},
    "preflight.py": {"tests/test_preflight_run_plan.py"},
    "processing.py": set(PROCESSING_TARGETED_TESTS),
    "processing_plan.py": {
        "tests/test_scan_background_stains.py",
        "tests/test_scan_processing_combo.py",
        "tests/test_scan_processing_reuse.py",
        "tests/test_scanline_lightening.py",
    },
    "processing_review.py": {"tests/test_processing_review.py"},
    "production_rehearsal.py": {"tests/test_production_rehearsal.py"},
    "production_review_queue.py": {"tests/test_production_review_queue.py", "tests/test_local_workbench_autosave.py"},
    "production_runner.py": {
        "tests/test_local_workbench_autosave.py",
        "tests/test_production_workbench_completion_handoff.py",
        "tests/test_quality_suite.py",
    },
    "public_capability_contract.py": {"tests/test_public_capability_contract.py"},
    "reports.py": {"tests/test_reports_contract.py"},
    "review_decisions.py": {"tests/test_local_workbench_autosave.py", "tests/test_review_decisions.py"},
    "rework.py": {"tests/test_rework_actions.py"},
    "rule_registry.py": {
        "tests/test_dat_10_2_deskew_post_verification.py",
        "tests/test_dat_10_3_crop_margin.py",
        "tests/test_dat_10_4_despeckle_preservation.py",
        "tests/test_rule_registry.py",
    },
    "rules.py": {
        "tests/test_dat_9_4_tiered_resolution.py",
        "tests/test_dat_10_2_deskew_post_verification.py",
        "tests/test_dat_10_4_despeckle_preservation.py",
        "tests/test_rule_registry.py",
        "tests/test_rules.py",
    },
    "run_plan.py": {"tests/test_preflight_run_plan.py"},
    "sampling.py": {"tests/test_dat_12_3_sampling_loop.py", "tests/test_sampling.py"},
    "scanner.py": {
        "tests/test_backend_consistency.py",
        "tests/test_content_type_regression.py",
        "tests/test_dat_9_4_tiered_resolution.py",
        "tests/test_dat_10_2_deskew_post_verification.py",
        "tests/test_dat_10_4_despeckle_preservation.py",
        "tests/test_deskew_optimization.py",
        "tests/test_despeckle_opencv_backend.py",
        "tests/test_scan_background_stains.py",
        "tests/test_scan_edge_shadow.py",
        "tests/test_scan_processing_combo.py",
        "tests/test_scan_processing_reuse.py",
        "tests/test_scan_processing_workflow_regression.py",
        "tests/test_scan_tone_normalization.py",
        "tests/test_scanline_lightening.py",
    },
    "service_api.py": {"tests/test_service_api.py"},
    "service_http.py": {"tests/test_service_http.py"},
    "service_jobs.py": {"tests/test_service_jobs.py"},
    "validation_index.py": {"tests/test_validation_index.py"},
    "workbench_summary.py": {"tests/test_workbench_summary.py"},
}


SCRIPT_TEST_MAP: dict[str, set[str]] = {
    "scripts/check_offline_dependencies.py": {"tests/test_delivery_tooling.py"},
    "scripts/ci_regression_groups.py": {"tests/test_ci_regression_groups.py"},
    "scripts/frontend_issue_driver.py": {"tests/test_delivery_tooling.py"},
    "scripts/generate_issue_plan.py": {"tests/test_delivery_tooling.py"},
    "scripts/release_candidate_summary.py": {"tests/test_release_summaries.py"},
    "scripts/release_readiness_summary.py": {"tests/test_release_summaries.py"},
    "scripts/run_dibco_external_cli_test.py": {"tests/test_ci_regression_groups.py"},
    "scripts/run_noisyoffice_external_cli_test.py": {"tests/test_ci_regression_groups.py"},
}


PROCESSING_KEYWORDS = (
    "background_stains",
    "edge_shadow",
    "tone_normalization",
    "scanline",
)


def select_tests_for_source_path(path: str) -> set[str]:
    file_name = Path(path).name
    tests = set(SOURCE_TEST_MAP.get(file_name, FAST_CONTRACT_TESTS))
    if any(token in path for token in PROCESSING_KEYWORDS):
        tests.update(
            {
                "tests/test_scan_background_stains.py",
                "tests/test_scan_edge_shadow.py",
                "tests/test_scan_tone_normalization.py",
                "tests/test_scanline_lightening.py",
            }
        )
    return tests


def _changed_files(base_ref: str) -> list[str]:
    output = subprocess.run(
        ["git", "diff", "--name-only", base_ref],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [line.strip() for line in output.splitlines() if line.strip()]


def _diff_for_path(base_ref: str, path: str) -> str:
    return subprocess.run(
        ["git", "diff", "--unified=0", base_ref, "--", path],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def parse_added_test_defs(diff_text: str) -> set[str]:
    names: set[str] = set()
    for line in diff_text.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        m = re.search(r"\bdef\s+(test_[A-Za-z0-9_]+)\s*\(", line)
        if m:
            names.add(m.group(1))
    return names


def parse_changed_new_line_numbers(diff_text: str) -> set[int]:
    changed: set[int] = set()
    new_line = 0
    in_hunk = False
    hunk_has_addition = False
    hunk_anchor = 0
    for line in diff_text.splitlines():
        match = HUNK_RE.match(line)
        if match:
            if in_hunk and not hunk_has_addition and hunk_anchor > 0:
                changed.add(hunk_anchor)
                if hunk_anchor > 1:
                    changed.add(hunk_anchor - 1)
            in_hunk = True
            new_line = int(match.group(1))
            hunk_anchor = new_line
            hunk_has_addition = False
            continue
        if not in_hunk:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            changed.add(new_line)
            hunk_has_addition = True
            new_line += 1
            continue
        if line.startswith("-") and not line.startswith("---"):
            continue
        if line.startswith("\\"):
            continue
        new_line += 1
    if in_hunk and not hunk_has_addition and hunk_anchor > 0:
        changed.add(hunk_anchor)
        if hunk_anchor > 1:
            changed.add(hunk_anchor - 1)
    return changed


def collect_test_method_spans(path: Path) -> list[MethodSpan]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    spans: list[MethodSpan] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for child in node.body:
            if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not child.name.startswith("test_"):
                continue
            end = getattr(child, "end_lineno", child.lineno)
            spans.append(
                MethodSpan(
                    class_name=node.name,
                    method_name=child.name,
                    start=child.lineno,
                    end=end,
                )
            )
    return spans


def select_test_ids_for_changed_test_file(path: str, diff_text: str) -> tuple[set[str], bool]:
    file_path = Path(path)
    if not file_path.exists():
        return set(), False

    module = file_path.stem
    try:
        spans = collect_test_method_spans(file_path)
    except (SyntaxError, UnicodeDecodeError):
        return set(), True

    added_names = parse_added_test_defs(diff_text)
    changed_lines = parse_changed_new_line_numbers(diff_text)

    selected_suffixes: set[str] = set()
    changed_inside_any_test = False

    for span in spans:
        if span.method_name in added_names:
            selected_suffixes.add(span.test_id)
        if any(span.start <= line <= span.end for line in changed_lines):
            selected_suffixes.add(span.test_id)
            changed_inside_any_test = True

    # if we touched the file but could not safely map non-empty changed lines to test methods, fallback to file.
    should_fallback = bool(changed_lines) and not changed_inside_any_test and not selected_suffixes
    return {f"{module}.{suffix}" for suffix in selected_suffixes}, should_fallback


def select_targeted_tests(base_ref: str, changed_files: list[str]) -> list[str]:
    tests: set[str] = set()
    for path in changed_files:
        if path.startswith("tests/test_") and path.endswith(".py"):
            diff_text = _diff_for_path(base_ref, path)
            selected_ids, fallback_file = select_test_ids_for_changed_test_file(path, diff_text)
            if selected_ids:
                tests.update(selected_ids)
            elif fallback_file:
                tests.add(path)

        if path.startswith("src/archive_scan_qc/"):
            tests.update(select_tests_for_source_path(path))
        if path in SCRIPT_TEST_MAP:
            tests.update(SCRIPT_TEST_MAP[path])

    existing_or_ids = []
    for test in sorted(tests):
        if test.endswith(".py") and not Path(test).exists():
            continue
        if test.startswith("tests/test_") and test.endswith(".py"):
            existing_or_ids.append(Path(test).stem)
        else:
            existing_or_ids.append(test)
    return existing_or_ids


def main() -> None:
    parser = argparse.ArgumentParser(description="Select PR targeted unittest ids/files")
    parser.add_argument("--base-ref", default="refs/remotes/ci/base..HEAD")
    parser.add_argument("--changed-files-file")
    args = parser.parse_args()

    if args.changed_files_file:
        changed_files = [
            line.strip()
            for line in Path(args.changed_files_file).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    else:
        changed_files = _changed_files(args.base_ref)

    for test in select_targeted_tests(args.base_ref, changed_files):
        print(test)


if __name__ == "__main__":
    main()
