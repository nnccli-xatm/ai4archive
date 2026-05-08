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
            report_dir / "scan_qc_report.json",
            report_dir / "scan_qc_report.html",
            report_dir / "scan_qc_files.csv",
            report_dir / "scan_qc_findings.csv",
            process_dir / "processing_manifest.json",
            process_dir / "images" / "BATCH001_PAGE_0001.png",
            process_dir / "images" / "BATCH001_PAGE_0002.png",
        ]
        missing = [path for path in expected if not path.exists()]
        if missing:
            raise SystemExit("missing example dry-run artifacts: " + ", ".join(str(path) for path in missing))
        report = json.loads((report_dir / "scan_qc_report.json").read_text(encoding="utf-8"))
        processing = json.loads((process_dir / "processing_manifest.json").read_text(encoding="utf-8"))
        if report["summary"]["total_files"] != 2 or report["summary"]["p0_findings"] != 0:
            raise SystemExit("example dry-run report did not complete cleanly")
        if report["manifest"]["rules_profile"]["name"] != "production-sample-standard":
            raise SystemExit("example dry-run did not load the sample rules profile")
        if not report["manifest"]["manifest_used"] or report["summary"]["manifest_missing_count"] != 0:
            raise SystemExit("example dry-run manifest compatibility check failed")
        if processing["summary"]["processed_files"] != 2 or processing["summary"]["failed_files"] != 0:
            raise SystemExit("example dry-run processing did not complete cleanly")


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
    run_install_smoke()
    print("release validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
