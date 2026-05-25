"""Aggregate sandbox-safe production workbench smoke suite.

The suite intentionally composes the narrower local smoke helpers and prints
only aggregate pass/fail evidence suitable for unattended PR validation.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
from dataclasses import dataclass
from typing import Any, Callable

from smoke_production_workbench_completion_export import run_smoke as run_completion_export_smoke
from smoke_production_workbench_operator_loop import run_smoke as run_operator_loop_smoke
from smoke_production_workbench_review_decisions import run_smoke as run_review_decisions_smoke
from smoke_production_workbench_start_preview import run_smoke as run_start_preview_smoke


PRIVATE_TERMS = {
    "/Users/",
    "/private/",
    "\\",
    ".jpg",
    ".jpeg",
    ".png",
    ".tif",
    ".tiff",
    "relative_path",
    "source_path",
    "thumbnail",
    "sha256",
    "hash",
    "OCR",
    "ocr_text",
    "preview_url",
    "original_path",
    "processed_path",
}


@dataclass(frozen=True)
class SmokeCheck:
    name: str
    run: Callable[[], dict[str, Any]]


CHECKS = [
    SmokeCheck("operator_first_closed_loop_entry", run_operator_loop_smoke),
    SmokeCheck("configure_start_preview", run_start_preview_smoke),
    SmokeCheck("preview_review_decisions", run_review_decisions_smoke),
    SmokeCheck("completion_export", run_completion_export_smoke),
]


def _assert_no_private_terms(payload: Any, label: str) -> None:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    leaked = sorted(term for term in PRIVATE_TERMS if term.lower() in text.lower())
    if leaked:
        raise AssertionError(f"{label} included non-aggregate evidence")


def run_suite() -> list[tuple[str, bool]]:
    results: list[tuple[str, bool]] = []
    for check in CHECKS:
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                evidence = check.run()
            _assert_no_private_terms(evidence, check.name)
        except Exception:
            results.append((check.name, False))
        else:
            results.append((check.name, True))
    return results


def main() -> int:
    results = run_suite()
    passed = sum(1 for _, ok in results if ok)
    failed = len(results) - passed

    print("Production workbench smoke suite")
    print(f"checks_run={len(results)} passed={passed} failed={failed}")
    for name, ok in results:
        print(f"{name}: {'pass' if ok else 'fail'}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
