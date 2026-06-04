"""AI4-863 performance optimization tests - Simple validation."""

import importlib.util
import json
from pathlib import Path
from PIL import Image
import hashlib
import sys
import unittest
import tempfile

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from archive_scan_qc.processing_plan import (
    build_processing_plan,
    _processing_options_fingerprint,
    _can_reuse_derivative,
    _compute_sha256_if_exists,
    write_processing_plan,
)
from archive_scan_qc.processing import ProcessingOptions


class TestAI4863Optimizations(unittest.TestCase):
    def test_processing_options_fingerprint_deterministic(self):
        """Verify processing options fingerprint is deterministic."""
        options1 = ProcessingOptions(
            deskew=True,
            auto_crop=True,
            trim_dark_border=True,
        )
        options2 = ProcessingOptions(
            deskew=True,
            auto_crop=True,
            trim_dark_border=True,
        )
        options3 = ProcessingOptions(
            deskew=False,
            auto_crop=True,
            trim_dark_border=True,
        )
        
        # Same options should produce same fingerprint
        fp1 = _processing_options_fingerprint(options1)
        fp2 = _processing_options_fingerprint(options2)
        fp3 = _processing_options_fingerprint(options3)
        
        self.assertIsInstance(fp1, str)
        self.assertEqual(len(fp1), 16)  # Should be first 16 chars of SHA256
        self.assertEqual(fp1, fp2)  # Same options -> same fingerprint
        self.assertNotEqual(fp1, fp3)  # Different options -> different fingerprint
    
    def test_compute_sha256_if_exists_file_exists(self):
        """Verify SHA256 computation for existing file."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            # Create a test file with known content
            test_file = tmp_path / "test.txt"
            test_file.write_text("known content", encoding="utf-8")
            
            # Compute SHA256
            sha256 = _compute_sha256_if_exists(test_file)
            
            # Should return a valid SHA256 hash
            self.assertIsInstance(sha256, str)
            self.assertEqual(len(sha256), 64)  # SHA256 is 64 hex chars
            
            # Verify it matches manual computation
            manual_hash = hashlib.sha256(b"known content").hexdigest()
            self.assertEqual(sha256, manual_hash)
    
    def test_compute_sha256_if_exists_file_missing(self):
        """Verify SHA256 computation returns None for missing file."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            missing_file = tmp_path / "missing.txt"
            sha256 = _compute_sha256_if_exists(missing_file)
            self.assertIsNone(sha256)
    
    def test_can_reuse_derivative_valid(self):
        """Verify derivative reuse validation for valid case."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            # Create test image
            source = tmp_path / "source.png"
            test_image = Image.new("RGB", (2000, 3000), color="white")
            test_image.save(source)
            source_hash = _compute_sha256_if_exists(source)
            
            # Create derivative
            derivative = tmp_path / "derivative.png"
            test_image.save(derivative)
            derivative_hash = _compute_sha256_if_exists(derivative)
            
            # Create previous record
            previous_record = {
                "status": "processed",
                "source_sha256": source_hash,
                "processing_options_fingerprint": "abc123def456",
                "output_relative_path": "derivative.png",
                "output_sha256": derivative_hash,
            }
            
            options = ProcessingOptions()
            options_fingerprint = _processing_options_fingerprint(options)
            
            # Modify previous record to match actual options fingerprint
            previous_record["processing_options_fingerprint"] = options_fingerprint
            
            # Should be reusable
            result = _can_reuse_derivative(source, previous_record, options, process_dir=tmp_path)
            self.assertTrue(result)

    def test_can_reuse_derivative_missing_output_file(self):
        """Verify derivative reuse validation fails when the derivative is missing."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            source = tmp_path / "source.png"
            Image.new("RGB", (2000, 3000), color="white").save(source)
            missing_derivative = tmp_path / "missing.png"

            options = ProcessingOptions()
            previous_record = {
                "status": "processed",
                "source_sha256": _compute_sha256_if_exists(source),
                "processing_options_fingerprint": _processing_options_fingerprint(options),
                "output_relative_path": missing_derivative.name,
                "output_sha256": hashlib.sha256(b"not-present").hexdigest(),
            }

            result = _can_reuse_derivative(source, previous_record, options, process_dir=tmp_path)
            self.assertFalse(result)

    def test_can_reuse_derivative_output_hash_mismatch(self):
        """Verify derivative reuse validation fails when the derivative digest changed."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            source = tmp_path / "source.png"
            derivative = tmp_path / "derivative.png"
            Image.new("RGB", (2000, 3000), color="white").save(source)
            Image.new("RGB", (2000, 3000), color="gray").save(derivative)

            options = ProcessingOptions()
            previous_record = {
                "status": "processed",
                "source_sha256": _compute_sha256_if_exists(source),
                "processing_options_fingerprint": _processing_options_fingerprint(options),
                "output_relative_path": derivative.name,
                "output_sha256": hashlib.sha256(b"different-output").hexdigest(),
            }

            result = _can_reuse_derivative(source, previous_record, options, process_dir=tmp_path)
            self.assertFalse(result)

    def test_can_reuse_derivative_rejects_output_outside_process_dir(self):
        """Verify derivative reuse validation rejects paths outside the processing directory."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            process_dir = tmp_path / "processed"
            outside_dir = tmp_path / "processed-other"
            process_dir.mkdir()
            outside_dir.mkdir()
            source = tmp_path / "source.png"
            derivative = outside_dir / "derivative.png"
            Image.new("RGB", (2000, 3000), color="white").save(source)
            Image.new("RGB", (2000, 3000), color="white").save(derivative)

            options = ProcessingOptions()
            previous_record = {
                "status": "processed",
                "source_sha256": _compute_sha256_if_exists(source),
                "processing_options_fingerprint": _processing_options_fingerprint(options),
                "output_relative_path": "../processed-other/derivative.png",
                "output_sha256": _compute_sha256_if_exists(derivative),
            }

            result = _can_reuse_derivative(source, previous_record, options, process_dir=process_dir)
            self.assertFalse(result)
    
    def test_can_reuse_derivative_invalid_status(self):
        """Verify derivative reuse validation fails for invalid status."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            source = tmp_path / "source.png"
            test_image = Image.new("RGB", (2000, 3000), color="white")
            test_image.save(source)
            
            # Create derivative
            derivative = tmp_path / "derivative.png"
            test_image.save(derivative)
            
            # Create previous record with failed status
            previous_record = {
                "status": "failed",  # Invalid status
                "source_sha256": _compute_sha256_if_exists(source),
                "processing_options_fingerprint": _processing_options_fingerprint(ProcessingOptions()),
                "output_relative_path": "derivative.png",
                "output_sha256": _compute_sha256_if_exists(derivative),
            }
            
            options = ProcessingOptions()
            
            # Should not be reusable
            result = _can_reuse_derivative(source, previous_record, options)
            self.assertFalse(result)
    
    def test_can_reuse_derivative_source_changed(self):
        """Verify derivative reuse validation fails when source changes."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            source = tmp_path / "source.png"
            test_image = Image.new("RGB", (2000, 3000), color="white")
            test_image.save(source)
            
            # Create derivative
            derivative = tmp_path / "derivative.png"
            test_image.save(derivative)
            
            # Create previous record with wrong source hash
            previous_record = {
                "status": "processed",
                "source_sha256": "wrong_hash_0000000000000000000000000000000000000000000000000000000000000000",
                "processing_options_fingerprint": _processing_options_fingerprint(ProcessingOptions()),
                "output_relative_path": "derivative.png",
                "output_sha256": _compute_sha256_if_exists(derivative),
            }
            
            options = ProcessingOptions()
            
            # Should not be reusable
            result = _can_reuse_derivative(source, previous_record, options)
            self.assertFalse(result)
    
    def test_can_reuse_derivative_options_changed(self):
        """Verify derivative reuse validation fails when options change."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            source = tmp_path / "source.png"
            test_image = Image.new("RGB", (2000, 3000), color="white")
            test_image.save(source)
            
            # Create derivative
            derivative = tmp_path / "derivative.png"
            test_image.save(derivative)
            
            # Create previous record with wrong options fingerprint
            previous_record = {
                "status": "processed",
                "source_sha256": _compute_sha256_if_exists(source),
                "processing_options_fingerprint": "wrong_fingerprint",
                "output_relative_path": "derivative.png",
                "output_sha256": _compute_sha256_if_exists(derivative),
            }
            
            options = ProcessingOptions()
            
            # Should not be reusable
            result = _can_reuse_derivative(source, previous_record, options)
            self.assertFalse(result)
    
    def test_build_processing_plan_new_functionality(self):
        """Verify build_processing_plan works with new parameters."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            # Create test image
            input_dir = tmp_path / "input"
            input_dir.mkdir()
            test_image = Image.new("RGB", (2000, 3000), color="white")
            test_image.save(input_dir / "test.png")
            
            # Create scan report
            report = {
                "schema_version": "scan-qc.report.v1",
                "generated_at": "2025-06-04T00:00:00Z",
                "project": {"id": "test", "name": "test"},
                "files": [
                    {
                        "relative_path": "test.png",
                        "sha256": _compute_sha256_if_exists(input_dir / "test.png"),
                        "openable": True,
                        "width": 2000,
                        "height": 3000,
                    }
                ]
            }
            
            # Build plan with new parameters
            plan = build_processing_plan(
                report,
                input_dir,
                ProcessingOptions(deskew=True),
                process_dir=None,  # New parameter
            )
            
            # Should create valid plan
            self.assertEqual(plan["schema_version"], "scan-qc.processing-plan.v1")
            self.assertEqual(plan["summary"]["planned_files"], 1)
            self.assertEqual(len(plan["files"]), 1)
            
            # Record should have new fields
            record = plan["files"][0]
            self.assertIn("scan_measurements_reused", record)
            self.assertIn("scan_measurement_reuse_reason", record)
            self.assertIn("existing_derivative_reused", record)
    
    def test_write_processing_plan_new_functionality(self):
        """Verify write_processing_plan works with new parameters."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            # Create test image
            input_dir = tmp_path / "input"
            input_dir.mkdir()
            test_image = Image.new("RGB", (2000, 3000), color="white")
            test_image.save(input_dir / "test.png")
            
            # Create scan report
            report = {
                "schema_version": "scan-qc.report.v1",
                "generated_at": "2025-06-04T00:00:00Z",
                "project": {"id": "test", "name": "test"},
                "files": [
                    {
                        "relative_path": "test.png",
                        "sha256": _compute_sha256_if_exists(input_dir / "test.png"),
                        "openable": True,
                        "width": 2000,
                        "height": 3000,
                    }
                ]
            }
            
            # Write report to file
            report_path = tmp_path / "report.json"
            report_path.write_text(json.dumps(report), encoding="utf-8")
            
            # Write plan with new parameters
            json_path, csv_path, plan = write_processing_plan(
                report_path,
                input_dir,
                tmp_path / "output",
                ProcessingOptions(deskew=True),
                process_dir=None,  # New parameter
            )
            
            # Should create valid plan and files
            self.assertTrue(json_path.exists())
            self.assertTrue(csv_path.exists())
            self.assertEqual(plan["schema_version"], "scan-qc.processing-plan.v1")
            
            # CSV should have new columns
            csv_content = csv_path.read_text(encoding="utf-8")
            self.assertIn("scan_measurements_reused", csv_content)
            self.assertIn("scan_measurement_reuse_reason", csv_content)
            self.assertIn("existing_derivative_reused", csv_content)

    def test_measure_resume_processing_seeds_output_from_initial_manifest(self):
        """Verify resume benchmark measures reuse from the prior processing output."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            previous_dir = tmp_path / "previous"
            previous_images = previous_dir / "images"
            previous_images.mkdir(parents=True)
            initial_manifest = previous_dir / "processing_manifest.json"
            initial_manifest.write_text(
                json.dumps({"schema_version": "scan-qc.processing.v1", "files": []}),
                encoding="utf-8",
            )
            (previous_images / "derivative.png").write_bytes(b"synthetic derivative")

            scan_report = tmp_path / "scan_report.json"
            scan_report.write_text(json.dumps({"files": []}), encoding="utf-8")
            input_dir = tmp_path / "input"
            input_dir.mkdir()
            output_dir = tmp_path / "resume"

            script_path = Path(__file__).parent.parent / "scripts" / "measure_ai4_863_performance.py"
            spec = importlib.util.spec_from_file_location("measure_ai4_863_performance_for_test", script_path)
            self.assertIsNotNone(spec)
            self.assertIsNotNone(spec.loader)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            def fake_process_images(report, passed_input_dir, passed_output_dir, options):
                self.assertEqual(report, {"files": []})
                self.assertEqual(passed_input_dir, input_dir)
                self.assertEqual(passed_output_dir, output_dir)
                self.assertTrue(options.resume_processing)
                self.assertTrue((output_dir / "processing_manifest.json").exists())
                self.assertTrue((output_dir / "images" / "derivative.png").exists())
                return {
                    "summary": {
                        "processed_files": 0,
                        "resumed_files": 1,
                        "existing_derivative_reused_files": 1,
                        "performance": {"processed_files_per_minute": 0.0},
                        "workers": 2,
                    }
                }

            module.process_images = fake_process_images
            result = module.measure_resume_processing(initial_manifest, scan_report, input_dir, output_dir, workers=2)

            self.assertEqual(result["files_resumed"], 1)
            self.assertEqual(result["existing_derivative_reused_files"], 1)


if __name__ == "__main__":
    unittest.main()
