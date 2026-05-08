#!/usr/bin/env python3
"""Run local release validation without project-specific service dependencies."""

from __future__ import annotations

import argparse
import compileall
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import venv


REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES_DIR = REPO_ROOT / "examples"
EXAMPLE_RULES_PROFILE = EXAMPLES_DIR / "rules-profile.production-sample.json"
EXAMPLE_MANIFEST = EXAMPLES_DIR / "manifest.sample.csv"


def _run(command: list[str], *, env: dict[str, str] | None = None, cwd: Path = REPO_ROOT) -> None:
    print("+ " + " ".join(command), flush=True)
    subprocess.run(command, cwd=cwd, env=env, check=True)


def _pythonpath_env() -> dict[str, str]:
    env = os.environ.copy()
    src = str(REPO_ROOT / "src")
    env["PYTHONPATH"] = src if not env.get("PYTHONPATH") else src + os.pathsep + env["PYTHONPATH"]
    return env


def _venv_python(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _venv_console(venv_dir: Path, name: str) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / f"{name}.exe"
    return venv_dir / "bin" / name


def run_unit_tests() -> None:
    _run([sys.executable, "-m", "unittest", "discover", "-s", "tests"], env=_pythonpath_env())


def run_compileall() -> None:
    print("+ compileall -q src tests", flush=True)
    ok = compileall.compile_dir(REPO_ROOT / "src", quiet=1) and compileall.compile_dir(REPO_ROOT / "tests", quiet=1)
    if not ok:
        raise SystemExit("compileall failed")


def run_install_smoke() -> None:
    with tempfile.TemporaryDirectory(prefix="archive-scan-qc-release-") as temp_dir:
        temp = Path(temp_dir)
        venv_dir = temp / ".venv"
        venv.EnvBuilder(with_pip=True, clear=True).create(venv_dir)
        python = _venv_python(venv_dir)
        _run([str(python), "-m", "pip", "install", "--upgrade", "pip"])
        _run([str(python), "-m", "pip", "install", str(REPO_ROOT)])
        _run([str(python), "-m", "pip", "show", "ai4archive"])
        _run([str(_venv_console(venv_dir, "archive-scan-qc")), "--version"])

        sample_dir = temp / "sample"
        report_dir = temp / "report"
        sample_dir.mkdir()
        _run(
            [
                str(python),
                "-c",
                (
                    "from PIL import Image; "
                    "Image.new('RGB', (32, 24), 'white').save("
                    "r'" + str(sample_dir / "A001_0001.png") + "', dpi=(300, 300))"
                ),
            ]
        )
        _run(
            [
                str(python),
                "-m",
                "archive_scan_qc",
                "--input",
                str(sample_dir),
                "--out",
                str(report_dir),
                "--project",
                "release-smoke",
                "--batch",
                "synthetic",
            ]
        )
        provider = temp / "fake_provider.py"
        provider.write_text(
            "import json, sys\n"
            "for line in sys.stdin:\n"
            "    row = json.loads(line)\n"
            "    print(json.dumps({"
            "'type':'finding',"
            "'relative_path':row['relative_path'],"
            "'rule':'provider.release.smoke',"
            "'severity':'P2',"
            "'confidence':0.5,"
            "'message':'Synthetic release smoke provider finding.',"
            "'metadata':{'model':'fake-release-smoke'}"
            "}))\n",
            encoding="utf-8",
        )
        provider_report_dir = temp / "provider-report"
        _run(
            [
                str(python),
                "-m",
                "archive_scan_qc",
                "--input",
                str(sample_dir),
                "--out",
                str(provider_report_dir),
                "--project",
                "release-smoke",
                "--batch",
                "synthetic-provider",
                "--analysis-provider-command",
                f"{python} {provider}",
            ]
        )
        provider_report = json.loads((provider_report_dir / "scan_qc_report.json").read_text(encoding="utf-8"))
        if provider_report["summary"]["provider_findings"] != 1:
            raise SystemExit("provider release smoke did not record the fake provider finding")
        expected = [
            report_dir / "scan_qc_report.json",
            report_dir / "scan_qc_report.html",
            report_dir / "scan_qc_files.csv",
            report_dir / "scan_qc_findings.csv",
        ]
        missing = [path for path in expected if not path.exists()]
        if missing:
            raise SystemExit("missing smoke-test reports: " + ", ".join(str(path) for path in missing))


def run_examples_dry_run() -> None:
    with tempfile.TemporaryDirectory(prefix="archive-scan-qc-examples-") as temp_dir:
        temp = Path(temp_dir)
        input_dir = temp / "input"
        report_dir = temp / "reports"
        process_dir = temp / "processed"
        input_dir.mkdir()
        _run(
            [
                sys.executable,
                "-c",
                (
                    "from PIL import Image, ImageDraw; "
                    "root = r'" + str(input_dir) + "'; "
                    "img = Image.new('RGB', (240, 180), 'white'); "
                    "draw = ImageDraw.Draw(img); "
                    "draw.rectangle((24, 24, 216, 156), outline=(40, 40, 40), width=2); "
                    "draw.text((36, 44), 'SYNTHETIC PAGE 0001', fill=(20, 20, 20)); "
                    "draw.line((36, 76, 204, 76), fill=(20, 20, 20), width=2); "
                    "draw.line((36, 108, 190, 108), fill=(20, 20, 20), width=2); "
                    "img.save(root + '/BATCH001_PAGE_0001.png', dpi=(300, 300)); "
                    "img.transpose(Image.Transpose.FLIP_LEFT_RIGHT).save("
                    "root + '/BATCH001_PAGE_0002.png', dpi=(300, 300))"
                ),
            ]
        )
        _run(
            [
                sys.executable,
                "-m",
                "archive_scan_qc",
                "preflight",
                "--input",
                str(input_dir),
                "--out",
                str(report_dir),
                "--process-out",
                str(process_dir),
                "--auto-crop",
                "--deskew",
                "--trim-dark-border",
                "--despeckle",
                "--workers",
                "1",
                "--project",
                "release-candidate",
                "--batch",
                "synthetic-example",
                "--manifest-csv",
                str(EXAMPLE_MANIFEST),
                "--rules-profile",
                str(EXAMPLE_RULES_PROFILE),
            ],
            env=_pythonpath_env(),
        )
        _run(
            [
                sys.executable,
                "-m",
                "archive_scan_qc",
                "--input",
                str(input_dir),
                "--out",
                str(report_dir),
                "--process-out",
                str(process_dir),
                "--auto-crop",
                "--deskew",
                "--trim-dark-border",
                "--despeckle",
                "--workers",
                "1",
                "--project",
                "release-candidate",
                "--batch",
                "synthetic-example",
                "--manifest-csv",
                str(EXAMPLE_MANIFEST),
                "--rules-profile",
                str(EXAMPLE_RULES_PROFILE),
            ],
            env=_pythonpath_env(),
        )
        expected = [
            report_dir / "preflight_report.json",
            report_dir / "scan_qc_report.json",
            report_dir / "scan_qc_report.html",
            report_dir / "scan_qc_files.csv",
            report_dir / "scan_qc_findings.csv",
            process_dir / "processing_manifest.json",
            process_dir / "processing_audit_summary.json",
            process_dir / "processing_retry_manifest.json",
            process_dir / "images" / "BATCH001_PAGE_0001.png",
            process_dir / "images" / "BATCH001_PAGE_0002.png",
        ]
        missing = [path for path in expected if not path.exists()]
        if missing:
            raise SystemExit("missing example dry-run artifacts: " + ", ".join(str(path) for path in missing))
        report = json.loads((report_dir / "scan_qc_report.json").read_text(encoding="utf-8"))
        preflight = json.loads((report_dir / "preflight_report.json").read_text(encoding="utf-8"))
        processing = json.loads((process_dir / "processing_manifest.json").read_text(encoding="utf-8"))
        audit = json.loads((process_dir / "processing_audit_summary.json").read_text(encoding="utf-8"))
        if preflight["status"] != "pass" or preflight["manifest"]["missing_count"] != 0:
            raise SystemExit("example dry-run preflight did not complete cleanly")
        if report["summary"]["total_files"] != 2 or report["summary"]["p0_findings"] != 0:
            raise SystemExit("example dry-run report did not complete cleanly")
        if report["manifest"]["rules_profile"]["name"] != "production-sample-standard":
            raise SystemExit("example dry-run did not load the sample rules profile")
        if not report["manifest"]["manifest_used"] or report["summary"]["manifest_missing_count"] != 0:
            raise SystemExit("example dry-run manifest compatibility check failed")
        if processing["summary"]["processed_files"] != 2 or processing["summary"]["failed_files"] != 0:
            raise SystemExit("example dry-run processing did not complete cleanly")
        if audit["counts"]["total_files"] != 2 or not audit["privacy"]["aggregate_only"]:
            raise SystemExit("example dry-run audit summary did not complete cleanly")
        local_review_dir = process_dir / "local-review"
        _run(
            [
                sys.executable,
                "-m",
                "archive_scan_qc",
                "processing-review-package",
                "--manifest",
                str(process_dir / "processing_manifest.json"),
                "--out",
                str(local_review_dir),
            ],
            env=_pythonpath_env(),
        )
        local_review_json = local_review_dir / "processing_review_package.json"
        local_review_html = local_review_dir / "processing_review_package.html"
        if not local_review_json.exists() or not local_review_html.exists():
            raise SystemExit("example dry-run processing review package was not written")
        local_review = json.loads(local_review_json.read_text(encoding="utf-8"))
        local_review_html_text = local_review_html.read_text(encoding="utf-8")
        if local_review["privacy"]["aggregate_only"] or not local_review["privacy"]["local_only"]:
            raise SystemExit("processing review package privacy flags are incorrect")
        if "data:image" in local_review_html_text.lower() or "<img" in local_review_html_text.lower():
            raise SystemExit("processing review package embedded image data")
        _run(
            [
                sys.executable,
                "-m",
                "archive_scan_qc",
                "review-export",
                "--report",
                str(report_dir / "scan_qc_report.json"),
                "--out",
                str(report_dir / "review_template.csv"),
            ],
            env=_pythonpath_env(),
        )
        _run(
            [
                sys.executable,
                "-m",
                "archive_scan_qc",
                "review-summary",
                "--review",
                str(report_dir / "review_template.csv"),
                "--out",
                str(report_dir / "review_summary.json"),
            ],
            env=_pythonpath_env(),
        )
        _run(
            [
                sys.executable,
                "-m",
                "archive_scan_qc",
                "calibrate-rules",
                "--report",
                str(report_dir / "scan_qc_report.json"),
                "--review-summary",
                str(report_dir / "review_summary.json"),
                "--out",
                str(report_dir / "rules_calibration_summary.json"),
                "--write-suggested-profile",
                str(report_dir / "rules-profile.suggested.json"),
            ],
            env=_pythonpath_env(),
        )
        calibration_text = (report_dir / "rules_calibration_summary.json").read_text(encoding="utf-8")
        suggested_profile = json.loads((report_dir / "rules-profile.suggested.json").read_text(encoding="utf-8"))
        if "BATCH001_PAGE_0001.png" in calibration_text or "sha256" in calibration_text:
            raise SystemExit("example rule calibration leaked row-level content")
        if not suggested_profile.get("draft") or not suggested_profile.get("suggested"):
            raise SystemExit("example suggested profile was not marked draft/suggested")


def run_benchmark_validation() -> None:
    with tempfile.TemporaryDirectory(prefix="archive-scan-qc-benchmark-") as temp_dir:
        temp = Path(temp_dir)
        input_dir = temp / "input"
        benchmark_dir = temp / "benchmark"
        input_dir.mkdir()
        _run(
            [
                sys.executable,
                "-c",
                (
                    "from PIL import Image, ImageDraw; "
                    "root = r'" + str(input_dir) + "'; "
                    "img = Image.new('RGB', (96, 72), 'white'); "
                    "draw = ImageDraw.Draw(img); "
                    "draw.text((8, 8), 'SYNTHETIC BENCHMARK', fill=(20, 20, 20)); "
                    "img.save(root + '/BENCH_0001.png', dpi=(300, 300)); "
                    "img.save(root + '/BENCH_0002.png', dpi=(300, 300))"
                ),
            ]
        )
        _run(
            [
                sys.executable,
                "-m",
                "archive_scan_qc",
                "benchmark",
                "--input",
                str(input_dir),
                "--out",
                str(benchmark_dir),
                "--workers-list",
                "1,2",
                "--scan-only",
            ],
            env=_pythonpath_env(),
        )
        payload_text = (benchmark_dir / "benchmark_results.json").read_text(encoding="utf-8")
        payload = json.loads(payload_text)
        if "recommendations" not in payload or not payload["recommendations"]["scan_only"]:
            raise SystemExit("benchmark validation did not produce scan recommendations")
        if payload["recommendations"]["scan_only"]["best_requested_workers"] not in {1, 2}:
            raise SystemExit("benchmark validation produced an unexpected recommended worker count")
        for forbidden in ["BENCH_0001.png", "BENCH_0002.png", '"files": [', '"findings": [', "relative_path", "sha256"]:
            if forbidden in payload_text:
                raise SystemExit(f"benchmark validation leaked row-level content: {forbidden}")


def run_acceptance_validation() -> None:
    with tempfile.TemporaryDirectory(prefix="archive-scan-qc-acceptance-") as temp_dir:
        temp = Path(temp_dir)
        evidence_dir = temp / "evidence"
        out_dir = temp / "acceptance"
        evidence_dir.mkdir()
        run_plan = {
            "schema_version": "scan-qc.run-plan-summary.v1",
            "privacy": {"aggregate_only": True},
            "summary": {
                "failed_batches": 0,
                "processing_failed_files": 0,
                "scan_files_per_minute": 120.0,
                "processing_files_per_minute": 75.0,
                "failed_batch_ids": ["PRIVATE_BATCH_SHOULD_NOT_APPEAR"],
            },
            "batches": [
                {
                    "batch_id": "PRIVATE_BATCH_SHOULD_NOT_APPEAR",
                    "report_dir": "/private/reports",
                    "process_out": "../private/process",
                    "workers": 2,
                }
            ],
        }
        review = {
            "schema_version": "scan-qc.review-summary.v1",
            "sensitivity": "Aggregate-only summary.",
            "total_findings": 0,
            "status_counts": {"pending": 0, "resolved": 0},
            "remaining_p0": 0,
            "remaining_p1": 0,
            "acceptance_passed": True,
        }
        audit = {
            "schema_version": "scan-qc.processing-audit.v1",
            "privacy": {"aggregate_only": True},
            "counts": {"failed_files": 0},
            "throughput": {"processed_files_per_minute": 78.0},
            "workers": {"effective_workers": 2},
        }
        benchmark = {
            "schema_version": "scan-qc.benchmark.v1",
            "privacy": {"aggregate_only": True},
            "runs": [
                {
                    "effective_workers": 2,
                    "scan": {"files_per_minute": 125.0},
                    "processing": {"failed_files": 0, "processed_files_per_minute": 80.0, "effective_workers": 2},
                }
            ],
        }
        (evidence_dir / "run_plan_summary.json").write_text(json.dumps(run_plan), encoding="utf-8")
        (evidence_dir / "review_summary.json").write_text(json.dumps(review), encoding="utf-8")
        (evidence_dir / "processing_audit_summary.json").write_text(json.dumps(audit), encoding="utf-8")
        (evidence_dir / "benchmark_results.json").write_text(json.dumps(benchmark), encoding="utf-8")
        _run(
            [
                sys.executable,
                "-m",
                "archive_scan_qc",
                "acceptance-summary",
                "--run-plan-summary",
                str(evidence_dir / "run_plan_summary.json"),
                "--review-summary",
                str(evidence_dir / "review_summary.json"),
                "--processing-audit-summary",
                str(evidence_dir / "processing_audit_summary.json"),
                "--benchmark-results",
                str(evidence_dir / "benchmark_results.json"),
                "--min-scan-files-per-minute",
                "100",
                "--min-processing-files-per-minute",
                "70",
                "--out",
                str(out_dir),
            ],
            env=_pythonpath_env(),
        )
        payload_text = (out_dir / "acceptance_summary.json").read_text(encoding="utf-8")
        payload = json.loads(payload_text)
        if payload["status"] != "pass" or payload["blocking_items"]:
            raise SystemExit("acceptance validation did not pass clean aggregate evidence")
        for forbidden in ["PRIVATE_BATCH_SHOULD_NOT_APPEAR", "/private/reports", "../private/process", "relative_path", "sha256"]:
            if forbidden in payload_text:
                raise SystemExit(f"acceptance validation leaked private content: {forbidden}")


def run_build_artifacts() -> None:
    build_dir = REPO_ROOT / "dist"
    if build_dir.exists():
        shutil.rmtree(build_dir)
    _run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--wheel-dir", str(build_dir), str(REPO_ROOT)])
    wheels = sorted(build_dir.glob("ai4archive-*.whl"))
    if not wheels:
        raise SystemExit("wheel build did not produce dist/ai4archive-*.whl")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run release validation for archive-scan-qc.")
    parser.add_argument(
        "--skip-build-artifacts",
        action="store_true",
        help="Skip local wheel creation when pip build isolation is unavailable.",
    )
    args = parser.parse_args(argv)

    run_unit_tests()
    run_compileall()
    if not args.skip_build_artifacts:
        run_build_artifacts()
    run_examples_dry_run()
    run_benchmark_validation()
    run_acceptance_validation()
    run_install_smoke()
    print("release validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
