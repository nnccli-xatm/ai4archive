PYTHON ?= python3
TEST_PYTHONPATH ?= src:tests

FAST_TESTS := \
	test_acceptance \
	test_acceptance_summary_regression \
	test_analysis_provider \
	test_artifact_readiness \
	test_ci_regression_groups \
	test_ci_targeted_selector \
	test_cli_smoke \
	test_cli_stable_contract \
	test_capability_probe \
	test_deep_inspection_candidates \
	test_deep_inspection_provider \
	test_delivery_tooling \
	test_dat_9_4_tiered_resolution \
	test_dat_10_2_deskew_post_verification \
	test_dat_10_3_crop_margin \
	test_dat_10_4_despeckle_preservation \
	test_dat_12_3_acceptance_verdict \
	test_dat_12_3_sampling_loop \
	test_evidence_bundle \
	test_final_handoff \
	test_handoff_manifest \
	test_image_processing_capability_smoke \
	test_manifest \
	test_processing_review \
	test_public_capability_contract \
	test_release_summaries \
	test_rework_actions \
	test_reports_contract \
	test_rule_registry \
	test_review_decisions \
	test_rules \
	test_rules_calibration \
	test_sampling \
	test_validation_index \
	test_workbench_summary \
	test_worker_recommendation

IMAGE_TESTS := \
	test_backend_consistency \
	test_content_type_regression \
	test_deskew_optimization \
	test_despeckle_opencv_backend \
	test_image_io_vips_backend \
	test_quality_suite \
	test_scan_background_stains \
	test_scan_edge_shadow \
	test_scan_processing_combo \
	test_scan_processing_reuse \
	test_scan_processing_workflow_regression \
	test_scan_tone_normalization \
	test_scanline_lightening

PLATFORM_TESTS := \
	test_local_workbench_autosave \
	test_preflight_run_plan \
	test_production_rehearsal \
	test_production_review_queue \
	test_production_workbench_completion_handoff \
	test_production_workbench_regression_guards

PERF_TESTS := test_performance_suite

DEEP_REGRESSION_TESTS := \
	test_scan_qc \
	test_scan_processing_algorithm_regression

.PHONY: test test-fast test-image test-platform test-perf test-deep-regression \
	test-core-image-processing test-production-cli test-privacy-boundary \
	test-external-validation test-regression-groups compile validate-release

test:
	PYTHONPATH=src $(PYTHON) -m unittest discover -s tests

test-fast:
	PYTHONPATH=$(TEST_PYTHONPATH) $(PYTHON) -m unittest $(FAST_TESTS)

test-image:
	PYTHONPATH=$(TEST_PYTHONPATH) $(PYTHON) -m unittest $(IMAGE_TESTS)

test-platform:
	PYTHONPATH=$(TEST_PYTHONPATH) $(PYTHON) -m unittest $(PLATFORM_TESTS)

test-perf:
	PYTHONPATH=$(TEST_PYTHONPATH) $(PYTHON) -m unittest $(PERF_TESTS)

test-deep-regression:
	PYTHONPATH=$(TEST_PYTHONPATH) $(PYTHON) -m unittest $(DEEP_REGRESSION_TESTS)

test-core-image-processing:
	PYTHONPATH=$(TEST_PYTHONPATH) $(PYTHON) scripts/ci_regression_groups.py run core-image-processing

test-production-cli:
	PYTHONPATH=$(TEST_PYTHONPATH) $(PYTHON) scripts/ci_regression_groups.py run production-cli

test-privacy-boundary:
	PYTHONPATH=$(TEST_PYTHONPATH) $(PYTHON) scripts/ci_regression_groups.py run privacy-boundary

test-external-validation:
	PYTHONPATH=$(TEST_PYTHONPATH) $(PYTHON) scripts/ci_regression_groups.py run external-validation

test-regression-groups:
	PYTHONPATH=$(TEST_PYTHONPATH) $(PYTHON) scripts/ci_regression_groups.py verify-coverage
	$(MAKE) test-core-image-processing PYTHON=$(PYTHON) TEST_PYTHONPATH=$(TEST_PYTHONPATH)
	$(MAKE) test-production-cli PYTHON=$(PYTHON) TEST_PYTHONPATH=$(TEST_PYTHONPATH)
	$(MAKE) test-privacy-boundary PYTHON=$(PYTHON) TEST_PYTHONPATH=$(TEST_PYTHONPATH)
	$(MAKE) test-external-validation PYTHON=$(PYTHON) TEST_PYTHONPATH=$(TEST_PYTHONPATH)

compile:
	PYTHONPATH=$(TEST_PYTHONPATH) $(PYTHON) -m compileall -q src tests

validate-release:
	$(PYTHON) scripts/validate_release.py
