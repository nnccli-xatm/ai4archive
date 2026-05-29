#!/usr/bin/env python3
"""Build offline wheelhouse, generate SBOM, and collect license evidence."""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.metadata
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = "scan-qc.offline-package.v1"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build offline package with wheelhouse, SBOM, and license evidence.")
    parser.add_argument("--wheelhouse", default="wheelhouse", help="Output directory for wheel files.")
    parser.add_argument("--sbom-output", default="offline_package/sbom.json", help="Output path for SBOM JSON.")
    parser.add_argument("--license-output", default="offline_package/licenses.json", help="Output path for license summary.")
    parser.add_argument("--bundle-output", default="offline_package/manifest.json", help="Output path for offline package manifest.")
    parser.add_argument("--skip-wheelhouse", action="store_true", help="Skip wheelhouse build (use existing).")
    parser.add_argument("--extra-deps", nargs="*", default=None, help="Additional dependency groups to include (numpy, opencv, vips, perf).")
    args = parser.parse_args(argv)

    project_root = Path(__file__).resolve().parent.parent

    if not args.skip_wheelhouse:
        print("Building wheelhouse...")
        wheelhouse = Path(args.wheelhouse)
        wheelhouse.mkdir(parents=True, exist_ok=True)
        cmd = [sys.executable, "-m", "pip", "wheel", "--wheel-dir", str(wheelhouse), "--no-deps", str(project_root)]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"Wheel build failed: {result.stderr}", file=sys.stderr)
            return 1

        deps_cmd = [sys.executable, "-m", "pip", "download", "--only-binary=:all:", "--dest", str(wheelhouse)]
        deps_cmd.append("Pillow>=10,<13")
        if args.extra_deps:
            extra_map = {
                "numpy": ["numpy>=1.21"],
                "opencv": ["opencv-python-headless>=4.5"],
                "vips": ["pyvips>=2.2"],
                "perf": ["numpy>=1.21", "opencv-python-headless>=4.5", "pyvips>=2.2"],
            }
            for dep in args.extra_deps:
                deps_cmd.extend(extra_map.get(dep, []))

        result = subprocess.run(deps_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"Dependency download failed: {result.stderr}", file=sys.stderr)
            return 1
        print(f"Wheelhouse built at {wheelhouse}")

    print("Generating SBOM...")
    sbom = _generate_sbom()
    sbom_path = Path(args.sbom_output)
    sbom_path.parent.mkdir(parents=True, exist_ok=True)
    sbom_path.write_text(json.dumps(sbom, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"SBOM written to {sbom_path}")

    print("Collecting license evidence...")
    licenses = _collect_licenses()
    license_path = Path(args.license_output)
    license_path.parent.mkdir(parents=True, exist_ok=True)
    license_path.write_text(json.dumps(licenses, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Licenses written to {license_path}")

    print("Generating offline package manifest...")
    manifest = _build_manifest(sbom, licenses, wheelhouse=Path(args.wheelhouse))
    manifest_path = Path(args.bundle_output)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Manifest written to {manifest_path}")
    return 0


def _generate_sbom() -> dict:
    packages = []
    for dist in importlib.metadata.distributions():
        name = dist.metadata["Name"]
        version = dist.metadata["Version"]
        if not name or not version:
            continue
        packages.append({
            "name": name,
            "version": version,
            "license": dist.metadata.get("License-Expression") or dist.metadata.get("License", "UNKNOWN"),
            "summary": dist.metadata.get("Summary", ""),
            "home_page": dist.metadata.get("Home-Page", ""),
            "requires_python": dist.metadata.get("Requires-Python", ""),
        })

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tool": "scripts/build_offline_package.py",
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "privacy": {"aggregate_only": True, "contains_paths": False, "contains_hashes": False},
        "packages": sorted(packages, key=lambda p: p["name"].lower()),
        "package_count": len(packages),
    }


def _collect_licenses() -> dict:
    licenses = []
    for dist in importlib.metadata.distributions():
        name = dist.metadata["Name"]
        if not name:
            continue
        license_text = ""
        for file_info in dist.files or []:
            if file_info.name.lower() in {"license", "license.txt", "license.md", "copying", "copying.txt"}:
                try:
                    license_text = file_info.locate().read_text(encoding="utf-8", errors="replace")[:2000]
                except OSError:
                    pass
                break
        licenses.append({
            "name": name,
            "version": dist.metadata.get("Version", "UNKNOWN"),
            "license": dist.metadata.get("License-Expression") or dist.metadata.get("License", "UNKNOWN"),
            "license_text_available": bool(license_text),
        })

    return {
        "schema_version": "scan-qc.license-evidence.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "privacy": {"aggregate_only": True, "contains_paths": False},
        "licenses": sorted(licenses, key=lambda l: l["name"].lower()),
        "license_count": len(licenses),
    }


def _build_manifest(sbom: dict, licenses: dict, *, wheelhouse: Path) -> dict:
    wheel_files = sorted(wheelhouse.glob("*.whl")) if wheelhouse.exists() else []
    return {
        "schema_version": "scan-qc.offline-package-manifest.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "privacy": {"aggregate_only": True, "contains_paths": False},
        "python_version": sbom.get("python_version", ""),
        "wheel_count": len(wheel_files),
        "wheels": [w.name for w in wheel_files],
        "sbom_package_count": sbom.get("package_count", 0),
        "license_count": licenses.get("license_count", 0),
        "required_packages": ["Pillow"],
        "optional_packages": {
            "numpy": "numpy>=1.21",
            "opencv": "opencv-python-headless>=4.5",
            "vips": "pyvips>=2.2",
        },
    }


if __name__ == "__main__":
    raise SystemExit(main())
