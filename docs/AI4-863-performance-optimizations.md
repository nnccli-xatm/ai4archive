# AI4-863: Performance Optimizations - Reduce Repeated Image Decode and Low-Value Derivative Writes

## Summary

This document describes the performance optimizations implemented in AI4-863 to reduce redundant image decoding and derivative writes during scan-QC processing. The improvements primarily target scan measurement reuse during processing and resume processing scenarios.

## Problem Statement

Before AI4-863, the processing pipeline performed several redundant operations:

1. **Redundant Image Decoding**: The processing phase always opened and decoded images, even when the same measurements (skew, dark_border) were already captured during the scan phase.

2. **Redundant Detection Operations**: Skew detection, dark border detection, and other measurements were re-computed during processing, even though they were available in the scan report.

3. **Unnecessary Derivative Re-writes**: During resume processing, derivative files were rewritten even when the source image and processing options had not changed.

## Solution Implementation

### 1. Scan Measurement Reuse

**File Modified**: `src/archive_scan_qc/processing.py`

**Key Functions Modified/Enhanced**:
- `_scan_measurements_for_processing()`: Extracts skew angle, confidence, dark_border bbox, and other measurements from scan report
- `_safe_deskew_skip_from_scan_record()`: Reuses skew detection results when valid
- `_process_image()`: Integrates scan measurement reuse into main processing flow

**How It Works**:
1. When `reuse_scan_measurements` is enabled in ProcessingOptions, the processing checks if scan measurements are valid
2. If valid (image is openable, dimensions match, measurements are present), it uses them directly
3. Only if measurements are missing/invalid does it perform full detection
4. The scan measurement reuse is recorded in operation timing and audit logs
5. Specific operations are tracked: `skew_detect_reused_scan_measurement`, `dark_border_detect_reused_scan_measurement`

**Expected Impact**:
- 50-80% reduction in detection operations during processing with scan measurement reuse
- Significant speedup for large batches with high scan measurement reuse rate
- Automatic fallback to full detection when measurements are unavailable

### 2. Resume Processing with Derivative Reuse

**File Modified**: `src/archive_scan_qc/processing.py`

**Key Functions Enhanced**:
- `_load_previous_records()`: Loads previous processing manifest from process directory
- `_previous_record_is_current()`: Validates derivative freshness by checking source hash and processing options fingerprint
- `_processing_options_fingerprint()`: Creates SHA256 fingerprint of processing options to detect changes
- `process_images()`: Integrates derivative reuse into main processing flow

**How It Works**:
1. When `resume_processing` is enabled in ProcessingOptions, the processing loads previous records
2. For each file, it checks if the existing derivative is still valid by:
   - Comparing source file hash (changes if source file was modified)
   - Comparing processing options fingerprint (changes if any option was modified)
   - Verifying output file existence and hash match
3. If both match, the derivative is marked as reusable and processing is skipped
4. The derivative reuse status is recorded as `resumed` status in processing manifest
5. Processing audit tracks `skipped_due_to_resume` and `existing_derivative_reused_files` counts

**Expected Impact**:
- 30-50% reduction in derivative writes during resume processing
- Faster resume operations for large batches where most files are unchanged
- Automatic detection of source file or processing option changes

### 3. Processing Plan Audit Enhancement

**File Modified**: `src/archive_scan_qc/processing_plan.py`

**Key Functions Enhanced**:
- `write_processing_plan()`: Added `process_dir` parameter for derivative reuse validation
- `build_processing_plan()`: Added support for loading previous records and derivative reuse checking
- `_write_plan_csv()`: Added new audit fields to CSV output

**New Audit Fields**:
- `scan_measurements_reused`: Boolean flag indicating scan measurement reuse
- `scan_measurement_reuse_reason`: Reason code when reuse occurs
- `existing_derivative_reused`: Boolean flag indicating derivative reuse during planning

**How It Works**:
1. Processing plan generation can now validate derivative freshness when `process_dir` is provided
2. Audit fields track both scan measurement and derivative reuse decisions
3. CSV output includes new columns for comprehensive tracking
4. Maintains full backward compatibility when new parameters are not provided

## Backward Compatibility

All changes are fully backward compatible:

1. **New Parameters Have Defaults**:
   - `process_dir` defaults to `None` in processing_plan functions
   - `resume_processing` defaults to `False` in ProcessingOptions
   - `reuse_scan_measurements` defaults to `False` in ProcessingOptions

2. **Existing Behavior Preserved**:
   - Callers that don't enable new flags maintain existing behavior
   - Processing still runs normally when reuse flags are disabled
   - Derivative reuse only activates when explicitly enabled

3. **CLI Unchanged**:
   - `--reuse-scan-measurements` flag already exists and works as expected
   - `--resume-processing` flag already exists and works as expected
   - No new CLI arguments required
   - Existing workflows continue to work without modification

## Performance Measurements

### Baseline Metrics (from AI4-862)
- Processing throughput: 111.61 files/minute
- Baseline measurement established on private validation sample

### Expected Improvements
- **Processing with reuse_scan_measurements**: 1.5-2x speedup (30-50% fewer detection operations)
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
   - Operations tracking: `skew_detect_reused_scan_measurement`, `dark_border_detect_reused_scan_measurement`
   - Operation timing includes `reused_scan_measurement` flag
   - Fallback reasons tracked when reuse is not possible
   - Aggregate summary includes `scan_measurement_reuse` counts

2. **Derivative Reuse**:
   - Processing manifest status changes to `resumed` for reused derivatives
   - Processing audit shows `skipped_due_to_resume` count
   - Processing summary includes `existing_derivative_reused_files` count
   - Detailed operation timing maintains full transparency

3. **CSV Export**:
   - Processing plan CSV includes `scan_measurements_reused`, `scan_measurement_reuse_reason`, `existing_derivative_reused`
   - Maintains compatibility with existing CSV consumers
   - New fields are additive only

## Usage Examples

### Enable Scan Measurement Reuse in Processing

```bash
archive-scan-qc preflight \
  --input /path/to/images \
  --process-out /path/to/derivatives \
  --reuse-scan-measurements \
  --deskew \
  --trim-dark-border \
  # ... other flags
```

### Enable Resume Processing in Production Run

The resume processing feature is integrated into the existing `--resume-processing` flag in run-plan and production workflows:

```bash
archive-scan-qc preflight \
  --input /path/to/images \
  --process-out /path/to/derivatives \
  --resume-processing \
  --reuse-scan-measurements \
  # ... other flags
```

## Testing

### Key Tests to Validate

1. **test_scan_processing_reuse.py** (18 tests):
   - Comprehensive coverage of scan measurement reuse
   - Derivative reuse validation
   - Processing option change detection
   - Fallback behavior verification

2. **test_ai4_863_optimizations.py** (9 tests):
   - Processing options fingerprint validation
   - Derivative reuse validation
   - Hash computation testing
   - New functionality integration tests

3. **Performance Measurement**:
   - Run `scripts/measure_ai4_863_performance.py` with private image data
   - Run `scripts/run_aggregate_baseline.py` with optimizations enabled
   - Compare against 111.61 files/minute baseline
   - Document actual performance improvements

## Maintenance Notes

1. **When Adding New Operations**: Consider if measurements can be reused from scan report
2. **When Modifying ProcessingOptions**: Update `_processing_options_fingerprint()` in processing.py to include new fields
3. **When Changing Scan Report Schema**: Update `_scan_measurements_for_processing()` to extract new fields
4. **Monitoring**: Track `scan_measurement_reuse` and `existing_derivative_reused` counts in production

## Related Issues

- AI4-862: Baseline performance measurements
- AI4-863: This issue (performance optimizations)
- Future: Consider similar optimizations for other processing phases

## Changelog

### Version 1.0.0 (AI4-863)
- Enhanced scan measurement reuse in processing.py with comprehensive validation
- Improved derivative reuse in processing.py with robust freshness checking
- Added processing plan audit enhancements in processing_plan.py
- Maintained full backward compatibility
- Added comprehensive audit trail for optimization decisions
- Added performance measurement scripts and unit tests
- Documentation created and updated to reflect actual implementation
