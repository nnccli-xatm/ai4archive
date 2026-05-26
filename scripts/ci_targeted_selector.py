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
    for line in diff_text.splitlines():
        match = HUNK_RE.match(line)
        if match:
            in_hunk = True
            new_line = int(match.group(1))
            continue
        if not in_hunk:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            changed.add(new_line)
            new_line += 1
            continue
        if line.startswith("-") and not line.startswith("---"):
            continue
        if line.startswith("\\"):
            continue
        new_line += 1
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

    module = path[:-3].replace("/", ".")
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
            tests.add("tests/test_scan_qc.py")
        if path.startswith("src/archive_scan_qc/") and "scan_processing" in path:
            tests.update(
                {
                    "tests/test_scan_processing_combo.py",
                    "tests/test_scan_processing_reuse.py",
                    "tests/test_scan_processing_algorithm_regression.py",
                }
            )
        if any(
            token in path
            for token in (
                "background_stains",
                "edge_shadow",
                "tone_normalization",
                "scanline",
            )
        ):
            tests.update(
                {
                    "tests/test_scan_background_stains.py",
                    "tests/test_scan_edge_shadow.py",
                    "tests/test_scan_tone_normalization.py",
                    "tests/test_scanline_lightening.py",
                }
            )

    existing_or_ids = []
    for test in sorted(tests):
        if test.endswith(".py") and not Path(test).exists():
            continue
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
