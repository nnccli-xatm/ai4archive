from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from archive_scan_qc.handoff import write_delivery_handoff_manifest


class HandoffManifestTests(unittest.TestCase):
    def test_delivery_handoff_manifest_classifies_and_hashes_artifacts(self) -> None:
            with tempfile.TemporaryDirectory() as temp_dir:
                temp = Path(temp_dir)
                report = temp / "scan_qc_report.json"
                benchmark = temp / "benchmark_results.json"
                unknown = temp / "operator_notes.txt"
                out_dir = temp / "handoff"
                report.write_text(
                    json.dumps({"schema_version": "scan-qc.phase1.v1", "files": [{"relative_path": "private/page.png"}]}),
                    encoding="utf-8",
                )
                benchmark.write_text(json.dumps({"schema_version": "scan-qc.benchmark.v1", "runs": []}), encoding="utf-8")
                unknown.write_text("local note", encoding="utf-8")

                json_path, csv_path, payload = write_delivery_handoff_manifest(
                    [
                        ("scan_report", report),
                        ("benchmark_results", benchmark),
                        ("artifact", unknown),
                    ],
                    out_dir,
                )

                self.assertTrue(json_path.exists())
                self.assertTrue(csv_path.exists())
                self.assertEqual(payload["schema_version"], "scan-qc.delivery-handoff-manifest.v1")
                self.assertEqual(payload["summary"]["artifact_count"], 3)
                self.assertEqual(payload["summary"]["aggregate_public_safe_count"], 1)
                self.assertEqual(payload["summary"]["sensitive_local_evidence_count"], 2)
                by_name = {record["name"]: record for record in payload["artifacts"]}
                self.assertEqual(by_name["benchmark_results.json"]["sensitivity"], "aggregate_public_safe")
                self.assertEqual(by_name["scan_qc_report.json"]["sensitivity"], "sensitive_local_evidence")
                self.assertEqual(by_name["operator_notes.txt"]["sensitivity"], "sensitive_local_evidence")
                self.assertEqual(by_name["benchmark_results.json"]["schema_version"], "scan-qc.benchmark.v1")
                self.assertRegex(by_name["scan_qc_report.json"]["sha256"], r"^[0-9a-f]{64}$")
                with csv_path.open(newline="", encoding="utf-8") as handle:
                    csv_rows = list(csv.DictReader(handle))
                self.assertEqual(len(csv_rows), 3)

    def test_delivery_handoff_manifest_rejects_missing_artifact(self) -> None:
            with tempfile.TemporaryDirectory() as temp_dir:
                temp = Path(temp_dir)
                with self.assertRaisesRegex(ValueError, "missing artifact"):
                    write_delivery_handoff_manifest([("scan_report", temp / "missing.json")], temp / "handoff")


if __name__ == "__main__":
    unittest.main()
