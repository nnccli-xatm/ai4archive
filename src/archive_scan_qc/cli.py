"""Command-line interface for phase-one scan QC."""

from __future__ import annotations

import argparse
from pathlib import Path

from .reports import write_reports
from .scanner import ScanConfig, scan_batch


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="archive-scan-qc",
        description="Run phase-one archive scan QC checks and write JSON/CSV reports.",
    )
    parser.add_argument("--input", required=True, type=Path, help="Image directory to scan.")
    parser.add_argument("--out", required=True, type=Path, help="Report output directory.")
    parser.add_argument("--project", default="default-project", help="Project identifier.")
    parser.add_argument("--batch", default="default-batch", help="Batch identifier.")
    parser.add_argument(
        "--min-dpi",
        default=200,
        type=int,
        help="Minimum acceptable horizontal and vertical DPI. Defaults to 200.",
    )
    parser.add_argument(
        "--name-pattern",
        default=None,
        help="Optional regular expression that each source file stem must match.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    config = ScanConfig(
        project_id=args.project,
        batch_id=args.batch,
        input_dir=args.input,
        output_dir=args.out,
        min_dpi=args.min_dpi,
        name_pattern=args.name_pattern,
    )
    report = scan_batch(config)
    paths = write_reports(report, args.out)

    print(f"Scanned {report['summary']['total_files']} files.")
    print(f"Openable: {report['summary']['openable_files']}")
    print(f"Findings: {report['summary']['total_findings']}")
    for label, path in paths.items():
        print(f"{label}: {path}")
    return 1 if report["summary"]["p0_findings"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
