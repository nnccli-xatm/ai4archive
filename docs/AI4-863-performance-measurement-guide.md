# AI4-863 Performance Measurement Execution Guide

## Quick Start for Private Validation

### Prerequisites
- Access to an operator-approved private validation set
- Python environment with archive_scan_qc installed
- Writeable output directory for generated artifacts

### Performance Measurement Steps

#### 1. Run Performance Script with Private Data
```bash
# From the repository root, set module loading to the local src directory.
# PowerShell:
$env:PYTHONPATH = (Resolve-Path .\src).Path

# Bash:
export PYTHONPATH="$(pwd)/src"

# Run comprehensive performance measurement
python scripts/measure_ai4_863_performance.py `
    --input /path/to/private/images `
    --out /path/to/performance/results `
    --workers 1 `
    --skip-processing  # Skip expensive full processing measurements
```

#### 2. Run Aggregate Baseline with Optimizations
```bash
# Run the aggregate baseline script with optimizations enabled
python scripts/run_aggregate_baseline.py `
    --input /path/to/private/images `
    --out /path/to/baseline/results `
    --reuse-scan-measurements `
    --resume-processing `
    --project "AI4-863-validation" `
    --batch "optimized" `
    --cleanup-artifacts
```

#### 3. Compare Against Baseline
- Review `aggregate_baseline_summary.json` for throughput metrics
- Compare `files_per_minute` against the current approved aggregate baseline
- Check `scan_measurement_reuse` and `existing_derivative_reused` counts

### Privacy Requirements

#### Allowed in Public Artifacts:
- Aggregate counts (total files, processed files, resumed files)
- Timing metrics (elapsed seconds, files per minute)
- Pass/fail status
- Reuse ratios and percentages
- Performance improvement factors

#### NOT Allowed in Public Artifacts:
- Real image filenames or paths
- File hashes (SHA256, MD5, etc.)
- Thumbnails or image content
- OCR text or row-level findings
- Any specific file-level details

### Expected Results

#### Scan Measurement Reuse:
- **Processing Plan Speedup**: 2-4x (50-80% fewer image decodes)
- **Operation Reuse**: Skew and dark border detection reused from scan report
- **Fallback Behavior**: Automatic fallback to full detection when measurements unavailable

#### Resume Processing:
- **Derivative Reuse**: 30-50% fewer derivative writes
- **Validation**: Source hash and options fingerprint verification
- **Skip Processing**: Automatic skip of unchanged derivatives

### Validation Criteria

#### Success Indicators:
1. **Zero Processing Failures**: All files processed successfully
2. **Measurable Throughput Improvement**: Above the approved aggregate baseline or documented reason
3. **Audit Trail Completeness**: All optimization decisions logged
4. **Backward Compatibility**: Existing workflows unchanged
5. **Privacy Compliance**: No private data in public artifacts

#### Failure Indicators:
1. Increased processing failures
2. Regression in quality or accuracy
3. Missing audit trail for optimization decisions
4. Exposure of private data in public comments/artifacts

### Troubleshooting

#### Common Issues:

**Issue**: Scan measurement reuse ratio is low
- **Check**: Scan report contains valid quality_skew_angle_degrees and quality_dark_border data
- **Solution**: Ensure scan is run with quality analysis enabled

**Issue**: Derivative reuse not working
- **Check**: Previous processing manifest exists and is valid
- **Check**: Source file hash matches previous record
- **Solution**: Ensure resume_processing flag is enabled and process_dir is set

**Issue**: Performance improvement not measurable
- **Check**: Test set size (too small for statistical significance)
- **Check**: Reuse ratios (low reuse = low impact)
- **Solution**: Use larger test sets or document no-gain reason with evidence

### Documentation Updates

After performance measurement, update this documentation:

1. **Actual Performance Numbers**: Replace expected ranges with measured values
2. **Reuse Statistics**: Document actual scan measurement and derivative reuse ratios
3. **Baseline Comparison**: Document comparison against the approved aggregate baseline
4. **Privacy Compliance**: Confirm all privacy requirements were met

### Next Steps

If validation passes:
1. Update documentation with actual performance data
2. Prepare PR description with performance evidence
3. Link to aggregate baseline results (privacy-safe only)
4. Submit for review

If validation fails:
1. Document the specific failure mode with evidence
2. Root cause analysis and fix if possible
3. Update documentation with blockers and limitations
4. Prepare for follow-up issue

## Related Files

- `docs/AI4-863-performance-optimizations.md` - Technical implementation details
- `scripts/measure_ai4_863_performance.py` - Performance measurement script
- `tests/test_ai4_863_optimizations.py` - Optimization unit tests
- `tests/test_scan_processing_reuse.py` - Scan measurement reuse tests
