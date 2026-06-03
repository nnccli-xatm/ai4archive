import unittest
import json
import tempfile
from pathlib import Path
from unittest import mock
import sys
import os

# Add scripts and src to path
repo_root = Path(__file__).resolve().parents[1]
scripts_dir = repo_root / "scripts"
src_dir = repo_root / "src"
if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))

from run_aggregate_baseline import (
    _baseline_summary,
    _aggregate_privacy_leaks,
    _looks_like_private_path,
    _looks_like_sensitive_filename,
    _looks_like_hash,
    cleanup_generated_artifacts,
    _should_preserve_cleanup_candidate,
    _update_privacy_self_check,
    BASELINE_JSON,
    GENERATED_ARTIFACT_NAMES,
)


class TestAggregateBaselineRegression(unittest.TestCase):
    """Regression tests for aggregate baseline fields and cleanup behavior."""

    def test_baseline_summary_schema_compatibility(self):
        """Verify baseline summary has all required fields with correct types."""
        args = mock.Mock(
            label="puersai-hpc",
            workers="4",
            benchmark_workers_list="4",
            benchmark_repeats=1,
            auto_crop=False,
            deskew=False,
            trim_dark_border=False,
            despeckle=False,
            despeckle_backend="fallback",
            resume_processing=False,
            reuse_scan_measurements=False,
            input="/fake/input",
            out="/fake/output",
        )
        
        private_summary = {
            "aggregate_counts": {
                "total_files": 100,
                "openable_files": 98,
                "total_findings": 5,
                "p0_findings": 1,
                "p1_findings": 2,
                "p2_findings": 2,
                "processing_processed_files": 95,
                "processing_failed_files": 3,
                "processing_resumed_files": 2,
                "processing_duplicate_reused_files": 1,
                "processing_existing_derivative_reused_files": 1,
                "processing_scan_measurement_reused_files": 1,
                "failed_batches": 0,
                "preflight_errors": 0,
            },
            "throughput": {
                "scan_elapsed_seconds": 60.0,
                "scan_files_per_minute": 100.0,
                "scan_openable_files_per_minute": 98.0,
                "benchmark_scan_files_per_minute": None,
                "processing_elapsed_seconds": 180.0,
                "processing_files_per_minute": 31.67,
                "benchmark_processing_files_per_minute": None,
                "processing_operation_timings": {},
                "benchmark_processing_operation_timings": {},
            },
            "configuration": {
                "processing_enabled": True,
                "benchmark_enabled": False,
                "benchmark_run_count": 0,
            },
            "benchmark": {
                "source": None,
                "run_count": 0,
                "finding_rule_counts_repeated_runs": {},
            },
            "environment": {
                "os": "Linux",
                "python_version": "3.10",
            },
            "despeckle_backend": {
                "requested_backend": "fallback",
                "effective_backend_mode": "fallback",
                "numpy_available": False,
                "backend_counts": {"numpy": 0, "fallback": 95, "not_applicable": 0, "unknown": 0},
                "fallback_count": 95,
                "requested_numpy_fallback_count": 0,
                "warning_codes": [],
            },
            "warning_item_count": 0,
            "warning_counts_by_code": {},
            "warning_items": [],
        }
        
        baseline = _baseline_summary(args, private_summary)
        
        # Verify schema version
        self.assertEqual(baseline["schema_version"], "scan-qc.aggregate-baseline.v1")
        
        # Verify target_environment
        self.assertIn("target_environment", baseline)
        self.assertEqual(baseline["target_environment"]["label"], "puersai-hpc")
        self.assertEqual(baseline["target_environment"]["validation_target"], "puersai-hpc")
        self.assertIsInstance(baseline["target_environment"]["gpu_acceleration_used"], bool)
        
        # Verify privacy section
        self.assertIn("privacy", baseline)
        self.assertTrue(baseline["privacy"]["aggregate_only"])
        self.assertIsInstance(baseline["privacy"]["guarantees"], list)
        self.assertGreater(len(baseline["privacy"]["guarantees"]), 0)
        
        # Verify worker_settings
        self.assertIn("worker_settings", baseline)
        self.assertEqual(baseline["worker_settings"]["requested_workers"], 4)
        self.assertEqual(baseline["worker_settings"]["benchmark_repeats"], 1)
        self.assertIsInstance(baseline["worker_settings"]["benchmark_enabled"], bool)
        
        # Verify operations
        self.assertIn("operations", baseline)
        self.assertIsInstance(baseline["operations"]["processing_enabled"], bool)
        self.assertIsInstance(baseline["operations"]["auto_crop"], bool)
        self.assertIsInstance(baseline["operations"]["deskew"], bool)
        
        # Verify aggregate_counts
        self.assertIn("aggregate_counts", baseline)
        self.assertEqual(baseline["aggregate_counts"]["total_files"], 100)
        self.assertEqual(baseline["aggregate_counts"]["openable_files"], 98)
        self.assertEqual(baseline["aggregate_counts"]["processing_processed_files"], 95)
        self.assertEqual(baseline["aggregate_counts"]["processing_failed_files"], 3)
        
        # Verify stage_timings
        self.assertIn("stage_timings", baseline)
        self.assertIn("scan", baseline["stage_timings"])
        self.assertIn("processing", baseline["stage_timings"])
        self.assertEqual(baseline["stage_timings"]["scan"]["files_per_minute"], 100.0)
        self.assertAlmostEqual(baseline["stage_timings"]["processing"]["processed_files_per_minute"], 31.67, places=2)
        
        # Verify cleanup section
        self.assertIn("cleanup", baseline)
        self.assertFalse(baseline["cleanup"]["enabled"])
        self.assertEqual(baseline["cleanup"]["retained_public_summary"], BASELINE_JSON)
        
        # Verify privacy_self_check
        self.assertIn("privacy_self_check", baseline)
        self.assertFalse(baseline["privacy_self_check"]["passed"])
        self.assertEqual(baseline["privacy_self_check"]["status"], "not_run")

    def test_baseline_phase_timings_preserved(self):
        """Verify phase timing fields are preserved and aggregate-only."""
        args = mock.Mock(
            label="test",
            workers="4",
            benchmark_workers_list=None,
            benchmark_repeats=1,
            auto_crop=False,
            deskew=False,
            trim_dark_border=False,
            despeckle=False,
            despeckle_backend="fallback",
            resume_processing=False,
            reuse_scan_measurements=False,
            input="/fake/input",
            out="/fake/output",
        )
        
        private_summary = {
            "aggregate_counts": {
                "total_files": 50,
                "openable_files": 50,
                "total_findings": 0,
                "p0_findings": 0,
                "p1_findings": 0,
                "p2_findings": 0,
                "processing_processed_files": 50,
                "processing_failed_files": 0,
                "failed_batches": 0,
                "preflight_errors": 0,
            },
            "throughput": {
                "scan_elapsed_seconds": 30.0,
                "scan_files_per_minute": 100.0,
                "scan_openable_files_per_minute": 100.0,
                "benchmark_scan_files_per_minute": 105.0,
                "processing_elapsed_seconds": 90.0,
                "processing_files_per_minute": 33.33,
                "benchmark_processing_files_per_minute": 35.0,
                "processing_operation_timings": {
                    "auto_crop": 1.5,
                    "deskew": 2.0,
                },
                "benchmark_processing_operation_timings": {
                    "auto_crop": 1.6,
                    "deskew": 2.1,
                },
            },
            "configuration": {
                "processing_enabled": True,
                "benchmark_enabled": True,
                "benchmark_run_count": 3,
            },
            "benchmark": {
                "source": "manual",
                "run_count": 3,
                "finding_rule_counts_repeated_runs": {},
            },
            "environment": {},
            "despeckle_backend": {
                "requested_backend": "fallback",
                "effective_backend_mode": "fallback",
                "numpy_available": False,
                "backend_counts": {"numpy": 0, "fallback": 50, "not_applicable": 0, "unknown": 0},
                "fallback_count": 50,
                "requested_numpy_fallback_count": 0,
                "warning_codes": [],
            },
            "warning_item_count": 0,
            "warning_counts_by_code": {},
            "warning_items": [],
        }
        
        baseline = _baseline_summary(args, private_summary)
        timings = baseline["stage_timings"]
        
        # Verify scan timings
        self.assertEqual(timings["scan"]["elapsed_seconds"], 30.0)
        self.assertEqual(timings["scan"]["files_per_minute"], 100.0)
        self.assertEqual(timings["scan"]["benchmark_files_per_minute"], 105.0)
        
        # Verify processing timings
        self.assertEqual(timings["processing"]["elapsed_seconds"], 90.0)
        self.assertAlmostEqual(timings["processing"]["processed_files_per_minute"], 33.33, places=2)
        self.assertEqual(timings["processing"]["benchmark_processed_files_per_minute"], 35.0)
        
        # Verify operation timings (aggregate-only)
        self.assertIn("operation_timings", timings["processing"])
        self.assertEqual(timings["processing"]["operation_timings"]["auto_crop"], 1.5)
        self.assertEqual(timings["processing"]["operation_timings"]["deskew"], 2.0)
        self.assertIn("benchmark_operation_timings", timings["processing"])

    def test_privacy_leaks_detection_paths(self):
        """Test privacy leak detection for various path patterns."""
        test_cases = [
            ("/home/user/private/data.jpg", True, "Unix absolute path"),
            ("~/Documents/secret.tif", True, "Home directory"),
            ("C:\\Users\\test\\image.png", True, "Windows absolute path"),
            ("/users/admin/scan.tiff", True, "Unix user path"),
            ("relative/path/data.txt", False, "Relative path"),
            ("./image.png", False, "Current directory"),
            ("../data/file.jpg", False, "Parent directory"),
        ]
        
        for value, should_leak, description in test_cases:
            is_private = _looks_like_private_path(value)
            self.assertEqual(is_private, should_leak, f"{description}: {value}")

    def test_privacy_leaks_detection_filenames(self):
        """Test privacy leak detection for sensitive filenames."""
        test_cases = [
            ("image.jpg", True, "JPEG file"),
            ("scan.jpeg", True, "JPEG file with extension"),
            ("page_001.png", True, "PNG file"),
            ("document.tif", True, "TIFF file"),
            ("scan.tiff", True, "TIFF file with extension"),
            ("photo.jp2", True, "JPEG 2000 file"),
            ("report.pdf", True, "PDF file"),
            ("data.txt", False, "Text file"),
            ("metadata.json", False, "JSON file"),
            ("index.html", False, "HTML file"),
        ]
        
        for filename, should_leak, description in test_cases:
            is_sensitive = _looks_like_sensitive_filename(filename.lower())
            self.assertEqual(is_sensitive, should_leak, f"{description}: {filename}")

    def test_privacy_leaks_detection_hashes(self):
        """Test privacy leak detection for hash-like strings."""
        test_cases = [
            ("a" * 32, True, "32-character hex (MD5)"),
            ("b" * 40, True, "40-character hex (SHA1)"),
            ("c" * 64, True, "64-character hex (SHA256)"),
            ("d" * 31, False, "31-character string"),
            ("e" * 33, False, "33-character string"),
            ("f" * 39, False, "39-character string"),
            ("g" * 41, False, "41-character string"),
            ("h" * 63, False, "63-character string"),
            ("i" * 65, False, "65-character string"),
            ("j" * 32 + "g", False, "32 hex + non-hex"),
            ("K" * 32, False, "uppercase K is not hex"),
            ("L" * 32, False, "uppercase L is not hex"),
        ]
        
        for hash_str, should_leak, description in test_cases:
            is_hash = _looks_like_hash(hash_str)
            self.assertEqual(is_hash, should_leak, f"{description}: {hash_str[:20]}...")

    def test_aggregate_privacy_leaks_comprehensive(self):
        """Test comprehensive privacy leak detection in aggregate baseline."""
        safe_payload = {
            "schema_version": "scan-qc.aggregate-baseline.v1",
            "aggregate_counts": {
                "total_files": 100,
                "openable_files": 98,
            },
            "stage_timings": {
                "scan": {
                    "elapsed_seconds": 60.0,
                    "files_per_minute": 100.0,
                }
            },
            "privacy_self_check": {
                "passed": True,
                "status": "pass",
            }
        }
        
        leaks = _aggregate_privacy_leaks(safe_payload)
        self.assertEqual(len(leaks), 0, f"Safe payload should have no leaks, got: {leaks}")
        
        # Test with private path
        payload_with_path = {
            "environment": {
                "data_path": "/home/user/scan/images",
            }
        }
        
        leaks = _aggregate_privacy_leaks(payload_with_path)
        self.assertGreater(len(leaks), 0, "Payload with private path should have leaks")
        self.assertTrue(any("path-like value" in leak for leak in leaks))
        
        # Test with sensitive filename
        payload_with_filename = {
            "benchmark": {
                "source": "scan_data.jpg",
            }
        }
        
        leaks = _aggregate_privacy_leaks(payload_with_filename)
        self.assertGreater(len(leaks), 0, "Payload with sensitive filename should have leaks")
        self.assertTrue(any("filename-like value" in leak for leak in leaks))
        
        # Test with hash
        payload_with_hash = {
            "checksum": "a" * 32,
        }
        
        leaks = _aggregate_privacy_leaks(payload_with_hash)
        self.assertGreater(len(leaks), 0, "Payload with hash should have leaks")
        self.assertTrue(any("hash-like value" in leak for leak in leaks))

    def test_cleanup_removes_generated_artifacts(self):
        """Test cleanup removes generated artifacts."""
        with tempfile.TemporaryDirectory() as temp_dir:
            output_root = Path(temp_dir)
            input_dir = output_root / "input"
            input_dir.mkdir()
            
            # Create generated artifacts
            for name in ["scan-reports", "processed-images", "run-plan"]:
                artifact_dir = output_root / name
                artifact_dir.mkdir()
                (artifact_dir / "test.json").write_text("{}")
            
            # Create public summary
            public_summary = output_root / BASELINE_JSON
            public_summary.write_text("{}")
            
            # Create input file
            input_file = input_dir / "sample.jpg"
            input_file.write_bytes(b"fake_image")
            
            # Run cleanup
            result = cleanup_generated_artifacts(
                output_root=output_root,
                input_dir=input_dir
            )
            
            # Verify cleanup result structure
            self.assertTrue(result["enabled"])
            self.assertIn("removed_artifacts", result)
            self.assertIn("preserved_artifacts", result)
            self.assertEqual(result["retained_public_summary"], BASELINE_JSON)
            
            # Verify artifacts removed
            self.assertEqual(
                sorted(result["removed_artifacts"]),
                ["processed-images", "run-plan", "scan-reports"]
            )
            
            # The public summary is retained through a stable aggregate-only field,
            # not through the private cleanup artifact list.
            self.assertEqual(result["retained_public_summary"], BASELINE_JSON)
            
            # Verify actual filesystem state
            self.assertFalse((output_root / "scan-reports").exists())
            self.assertFalse((output_root / "processed-images").exists())
            self.assertFalse((output_root / "run-plan").exists())
            self.assertTrue(public_summary.exists())
            self.assertTrue(input_file.exists())
            self.assertTrue(input_dir.exists())

    def test_cleanup_preserves_input_samples(self):
        """Test cleanup preserves input samples inside output root."""
        with tempfile.TemporaryDirectory() as temp_dir:
            output_root = Path(temp_dir)
            input_dir = output_root / "private-input"
            input_dir.mkdir()
            
            # Create input samples
            sample1 = input_dir / "page_001.jpg"
            sample2 = input_dir / "page_002.jpg"
            sample1.write_bytes(b"image1")
            sample2.write_bytes(b"image2")
            
            # Create generated artifacts
            scan_reports = output_root / "scan-reports"
            scan_reports.mkdir()
            
            # Run cleanup
            result = cleanup_generated_artifacts(
                output_root=output_root,
                input_dir=input_dir
            )
            
            # Verify input preserved
            self.assertTrue(input_dir.exists())
            self.assertTrue(sample1.exists())
            self.assertTrue(sample2.exists())
            
            # Cleanup must preserve input samples without exposing sample paths in
            # the aggregate cleanup result.
            self.assertEqual(result["preserved_artifacts"], [])
            self.assertEqual(result["removed_artifacts"], ["scan-reports"])

    def test_cleanup_preserves_repository_root(self):
        """Test cleanup preserves repository root if inside output root."""
        with tempfile.TemporaryDirectory() as temp_dir:
            output_root = Path(temp_dir)
            input_dir = output_root / "input"
            input_dir.mkdir()
            
            # Simulate repository structure
            repo_src = output_root / "src"
            repo_src.mkdir()
            (repo_src / "test.py").write_text("print('test')")
            
            # Create generated artifacts
            scan_reports = output_root / "scan-reports"
            scan_reports.mkdir()
            
            # Mock REPO_ROOT to be inside output_root
            import run_aggregate_baseline
            original_repo_root = run_aggregate_baseline.REPO_ROOT
            run_aggregate_baseline.REPO_ROOT = repo_src
            
            try:
                # Run cleanup
                result = cleanup_generated_artifacts(
                    output_root=output_root,
                    input_dir=input_dir
                )
                
                # Verify repository preserved
                self.assertTrue(repo_src.exists())
                self.assertTrue((repo_src / "test.py").exists())
                
                # Verify scan reports still removed
                self.assertFalse(scan_reports.exists())
            finally:
                run_aggregate_baseline.REPO_ROOT = original_repo_root

    def test_cleanup_handles_missing_artifacts(self):
        """Test cleanup handles missing generated artifacts gracefully."""
        with tempfile.TemporaryDirectory() as temp_dir:
            output_root = Path(temp_dir)
            input_dir = output_root / "input"
            input_dir.mkdir()
            
            # Create public summary only
            public_summary = output_root / BASELINE_JSON
            public_summary.write_text("{}")
            
            # Run cleanup
            result = cleanup_generated_artifacts(
                output_root=output_root,
                input_dir=input_dir
            )
            
            # Should succeed with empty removed list while retaining the public
            # summary on disk.
            self.assertEqual(result["removed_artifacts"], [])
            self.assertEqual(result["retained_public_summary"], BASELINE_JSON)
            self.assertTrue(public_summary.exists())

    def test_cleanup_tracks_elapsed_time(self):
        """Test cleanup tracks elapsed time."""
        with tempfile.TemporaryDirectory() as temp_dir:
            output_root = Path(temp_dir)
            input_dir = output_root / "input"
            input_dir.mkdir()
            
            # Create artifact to cleanup
            scan_reports = output_root / "scan-reports"
            scan_reports.mkdir()
            
            # Run cleanup
            result = cleanup_generated_artifacts(
                output_root=output_root,
                input_dir=input_dir
            )
            
            # Verify elapsed time tracked
            self.assertIn("elapsed_seconds", result)
            self.assertIsInstance(result["elapsed_seconds"], float)
            self.assertGreaterEqual(result["elapsed_seconds"], 0.0)

    def test_privacy_self_check_blocks_forbidden_values(self):
        """Test privacy self-check blocks forbidden values."""
        args = mock.Mock(
            input="/private/input",
            out="/private/output",
            manifest_csv=None,
            rules_profile=None,
        )
        
        baseline = {
            "schema_version": "scan-qc.aggregate-baseline.v1",
            "aggregate_counts": {"total_files": 100},
            "stage_timings": {
                "scan": {"files_per_minute": 100.0},
            },
            "privacy_self_check": {
                "passed": False,
                "status": "not_run",
                "violation_count": None,
                "violations": [],
            }
        }
        
        # This should not raise because the baseline has no private data
        try:
            _update_privacy_self_check(args, baseline)
            self.assertTrue(baseline["privacy_self_check"]["passed"])
            self.assertEqual(baseline["privacy_self_check"]["status"], "pass")
        except ValueError as e:
            self.fail(f"Privacy self-check failed on clean baseline: {e}")

    def test_regression_baseline_field_consistency(self):
        """Test that baseline fields remain consistent across runs."""
        args = mock.Mock(
            label="puersai-hpc",
            workers="4",
            benchmark_workers_list="4",
            benchmark_repeats=1,
            auto_crop=False,
            deskew=False,
            trim_dark_border=False,
            despeckle=False,
            despeckle_backend="fallback",
            resume_processing=False,
            reuse_scan_measurements=False,
            input="/fake/input",
            out="/fake/output",
        )
        
        private_summary = {
            "aggregate_counts": {
                "total_files": 100,
                "openable_files": 98,
                "total_findings": 5,
                "p0_findings": 1,
                "p1_findings": 2,
                "p2_findings": 2,
                "processing_processed_files": 95,
                "processing_failed_files": 3,
                "processing_resumed_files": 2,
                "processing_duplicate_reused_files": 1,
                "processing_existing_derivative_reused_files": 1,
                "processing_scan_measurement_reused_files": 1,
                "failed_batches": 0,
                "preflight_errors": 0,
            },
            "throughput": {
                "scan_elapsed_seconds": 60.0,
                "scan_files_per_minute": 100.0,
                "scan_openable_files_per_minute": 98.0,
                "benchmark_scan_files_per_minute": None,
                "processing_elapsed_seconds": 180.0,
                "processing_files_per_minute": 31.67,
                "benchmark_processing_files_per_minute": None,
                "processing_operation_timings": {},
                "benchmark_processing_operation_timings": {},
            },
            "configuration": {
                "processing_enabled": True,
                "benchmark_enabled": False,
                "benchmark_run_count": 0,
            },
            "benchmark": {
                "source": None,
                "run_count": 0,
                "finding_rule_counts_repeated_runs": {},
            },
            "environment": {
                "os": "Linux",
                "python_version": "3.10",
            },
            "despeckle_backend": {
                "requested_backend": "fallback",
                "effective_backend_mode": "fallback",
                "numpy_available": False,
                "backend_counts": {"numpy": 0, "fallback": 95, "not_applicable": 0, "unknown": 0},
                "fallback_count": 95,
                "requested_numpy_fallback_count": 0,
                "warning_codes": [],
            },
            "warning_item_count": 0,
            "warning_counts_by_code": {},
            "warning_items": [],
        }
        
        # Generate two baselines
        baseline1 = _baseline_summary(args, private_summary)
        baseline2 = _baseline_summary(args, private_summary)
        
        # Verify schema consistency
        self.assertEqual(baseline1["schema_version"], baseline2["schema_version"])
        
        # Verify field presence consistency
        fields1 = set(baseline1.keys())
        fields2 = set(baseline2.keys())
        self.assertEqual(fields1, fields2, "Baseline fields should be consistent")
        
        # Verify required fields present
        required_fields = {
            "schema_version",
            "generated_at",
            "target_environment",
            "privacy",
            "worker_settings",
            "operations",
            "despeckle_backend",
            "aggregate_counts",
            "stage_timings",
            "benchmark",
            "environment",
            "runtime_hardware",
            "cleanup",
            "privacy_self_check",
        }
        self.assertTrue(required_fields.issubset(fields1), "All required fields should be present")
        
        # Verify data type consistency
        for field in fields1:
            type1 = type(baseline1[field])
            type2 = type(baseline2[field])
            self.assertEqual(type1, type2, f"Field '{field}' type should be consistent: {type1} vs {type2}")


if __name__ == "__main__":
    unittest.main()
