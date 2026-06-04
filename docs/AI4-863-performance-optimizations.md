# AI4-863: Performance Optimizations - Reduce Repeated Image Decode and Low-Value Derivative Writes

## Summary

This document describes the performance optimizations implemented in AI4-863 to reduce redundant image decoding and derivative writes during scan-QC processing. The improvements primarily target processing plan generation and resume processing scenarios.

## Problem Statement

Before AI4-863, the processing pipeline performed several redundant operations:

1. **Redundant Image Decoding**: The processing plan phase always opened and decoded images, even when the same measurements (skew, dark_border) were already captured during the scan phase.

2. **Redundant Detection Operations**: Skew detection, dark border detection, and other measurements were re-computed during processing plan generation, even though they were available in the scan report.

3. **Unnecessary Derivative Re-writes**: During resume processing, derivative files were rewritten even when the source image and processing options had not changed.

4. **Redundant Hash Computations**: File hashes were computed multiple times across scan, plan, and processing phases.

## Solution Implementation

### 1. Scan Measurement Reuse

**File Modified**: `src/archive_scan_qc/processing_plan.py`

**Key Functions Added**:
- `_extract_scan_measurements()`: Extracts skew angle, confidence, dark_border bbox, and other measurements from scan report
- `_populate_from_scan_measurements()`: Populates plan records with scan measurements without image decode
- Conditional image decode in `_plan_record()`: Skips decode when measurements are valid

**How It Works**:
1. When `reuse_scan_measurements` is enabled, the processing plan checks if scan measurements are valid
2. If valid (image is openable, dimensions match, measurements are present), it uses them directly
3. Only if measurements are missing/invalid does it perform full image decode and re-detection
4. The scan measurement reuse reason is recorded for audit purposes

**Expected Impact**:
- 50-80% reduction in image decode operations during dry-run processing plan generation
- Significant speedup for large batches with high scan measurement reuse rate

### 2. Resume Processing Support

**Key Functions Added**:
- `_load_previous_records()`: Loads previous processing manifest from process directory
- `_can_reuse_derivative()`: Validates derivative freshness by checking source hash and processing options fingerprint
- `_processing_options_fingerprint()`: Creates SHA256 fingerprint of processing options to detect changes
- `_compute_sha256_if_exists()`: Computes hash only when file exists and is readable

**Parameters Added**:
- `process_dir`: Optional path to previous processing output directory
- `resume_processing`: Boolean flag to enable derivative reuse

**How It Works**:
1. When `resume_processing` is enabled and `process_dir` is provided, the plan loads previous processing records
2. For each file, it checks if the existing derivative is still valid by:
   - Comparing source file hash (changes if source file was modified)
   - Comparing processing options fingerprint (changes if any option was modified)
3. If both match, the derivative is marked as reusable and processing is skipped
4. The derivative reuse status is recorded for audit purposes

**Expected Impact**:
- 30-50% reduction in derivative writes during resume processing
- Faster resume operations for large batches where most files are unchanged

### 3. Hash Computation Optimization

**Key Function Added**:
- `_compute_sha256_if_exists()`: Computes hash only when file exists and is readable

**How It Works**:
- Returns `None` if file doesn't exist or isn't readable (instead of raising exception)
- Avoids redundant hash computations by caching results where possible
- Used for both source and derivative hash validation

**Expected Impact**:
- Reduced overhead in derivative reuse checks
- Fewer exceptions and error handling overhead

## Backward Compatibility

All changes are fully backward compatible:

1. **New Parameters Have Defaults**:
   - `process_dir` defaults to `None`
   - `resume_processing` defaults to `False` in ProcessingOptions
   - `reuse_scan_measurements` defaults to `False` in ProcessingOptions

2. **Existing Behavior Preserved**:
   - Callers that don't provide new parameters maintain existing behavior
   - Image decode still happens when measurements are invalid
   - Processing still runs normally when resume is disabled

3. **CLI Unchanged**:
   - `--reuse-scan-measurements` flag already exists and is reused
   - No new CLI arguments required
   - Existing workflows continue to work

## Performance Measurements

### Baseline Metrics (from AI4-862)
- Processing throughput: 111.61 files/minute
- Baseline measurement to be established

### Expected Improvements
- **Processing Plan with reuse_scan_measurements**: 2-4x speedup (50-80% fewer decodes)
- **Resume Processing**: 1.5-2x speedup (30-50% fewer derivative writes)
- **Overall Throughput**: Measurable improvement above 111.61 files/minute

### Validation Requirements
1. Processing failures remain zero on private validation sample
2. Processing audit output still explains operations, skipped files, and resume behavior
3. All existing tests pass, particularly `test_scan_processing_reuse.py`
4. Throughput improvement is measurable or no-gain reason is documented

## Audit Trail

All optimizations preserve the audit trail:

1. **Scan Measurement Reuse**:
   - Records `scan_measurements_reused: true` when reuse occurs
   - Records `scan_measurement_reuse_reason` with specific reason
   - Reason codes include: `scan_measurements_available`, `scan_record_not_openable`, `scan_dimensions_missing`

2. **Derivative Reuse**:
   - Records `existing_derivative_reused: true` when derivative is reused
   - Processing audit shows skip reason
   - Processing summary includes `existing_derivative_reused_files` count

3. **CSV Export**:
   - Added fields: `scan_measurements_reused`, `scan_measurement_reuse_reason`, `existing_derivative_reused`
   - Maintains compatibility with existing CSV consumers

## Usage Examples

### Enable Scan Measurement Reuse in Processing Plan

```bash
archive-scan-qc processing-plan \
  --report /path/to/scan_qc_report.json \
  --input /path/to/images \
  --out /path/to/processing-plan \
  --deskew \
  --trim-dark-border \
  --reuse-scan-measurements
```

### Enable Resume Processing in Production Run

The resume processing feature is integrated into the existing `--resume-processing` flag in run-plan and production workflows:

```bash
archive-scan-qc preflight \
  --input /path/to/images \
  --process-out /path/to/derivatives \
  --resume-processing \
  # ... other flags
```

## Testing

### Key Tests to Validate

1. **test_scan_processing_reuse.py**:
   - `test_processing_plan_reuses_scan_measurements_when_enabled`
   - `test_processing_plan_cli_accepts_reuse_scan_measurements`

2. **Regression Tests**:
   - Verify all existing tests pass
   - Check that processing plans are deterministic
   - Verify backward compatibility with existing workflows

3. **Performance Tests**:
   - Measure processing plan time with and without `reuse_scan_measurements`
   - Measure resume processing time with and without derivative reuse
   - Compare overall throughput against baseline

## Maintenance Notes

1. **When Adding New Operations**: Consider if measurements can be reused from scan report
2. **When Modifying ProcessingOptions**: Update `_processing_options_fingerprint()` to include new fields
3. **When Changing Scan Report Schema**: Update `_extract_scan_measurements()` to extract new fields
4. **Monitoring**: Track `scan_measurements_reused` and `existing_derivative_reused` counts in production

## Related Issues

- AI4-862: Baseline performance measurements
- AI4-863: This issue (performance optimizations)
- Future: Consider similar optimizations for other processing phases

## Changelog

### Version 1.0.0 (AI4-863)
- Added scan measurement reuse to processing plan generation
- Added resume processing support with derivative freshness checking
- Added hash computation optimization
- Maintained full backward compatibility
- Added audit trail for optimization decisions
- Updated CSV export with new fields
- Documentation created
