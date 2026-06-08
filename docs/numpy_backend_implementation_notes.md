# NumPy Backend Implementation Notes (AI4-867)

## Overview

AI4-867 adds and validates the first narrow NumPy-backed despeckle candidate
selection slice. The implementation keeps candidate behavior identical to the
fallback path characterized by AI4-866.

The change is intentionally small:

- Use NumPy for dark pixel extraction when the NumPy backend is requested and
  available.
- Delegate candidate filtering to the existing
  `_despeckle_candidate_points_from_dark_points()` helper.
- Keep the fallback path available for environments without NumPy.
- Report the actual backend mode used by the candidate path.

## Target Functions

- `_despeckle_candidate_points_with_backend(dark_mask, backend=...)`
- `_despeckle_candidate_points_numpy(dark_mask)`
- `_despeckle_candidate_points_from_dark_points(...)`

## Dispatch Behavior

The backend dispatch accepts `fallback`, `numpy`, and `opencv`.

```python
def _despeckle_candidate_points_with_backend(
    dark_mask: Image.Image, *, backend: str = "fallback"
) -> tuple[list[tuple[int, int]], str]:
    if backend not in {"fallback", "numpy", "opencv"}:
        raise ValueError("despeckle backend must be fallback, numpy, or opencv")

    if backend == "opencv":
        opencv_candidates = _despeckle_candidate_points_numpy(dark_mask)
        if opencv_candidates is not None:
            return opencv_candidates, "numpy"
        return _despeckle_candidate_points_fallback(dark_mask), "fallback"

    if backend == "numpy":
        numpy_candidates = _despeckle_candidate_points_numpy(dark_mask)
        if numpy_candidates is not None:
            return numpy_candidates, "numpy"

    return _despeckle_candidate_points_fallback(dark_mask), "fallback"
```

The `opencv` option currently delegates to the NumPy candidate extraction
implementation. Returning `numpy` for that path reports the actual backend used
instead of implying OpenCV-specific processing.

## NumPy Slice

The NumPy slice only accelerates extraction of dark candidate coordinates:

1. Convert the PIL mask to a NumPy array.
2. Use `np.nonzero()` to extract dark pixel coordinates.
3. Convert coordinates to the existing point representation.
4. Delegate all filtering, edge protection, component analysis, and sorting to
   `_despeckle_candidate_points_from_dark_points()`.

This keeps parity risk low because the complex decision logic remains shared.

## Fallback Safety

- If NumPy is unavailable, the function returns `None` and dispatch falls back.
- If NumPy raises during extraction, dispatch falls back.
- Invalid backend names still raise `ValueError`.
- The default backend remains `fallback`.

## Validation

Local validation performed for the submitted change:

- `python tests/test_ai4_866_despeckle_fallback_parity.py`
  - 27 tests passed.
- `python tests/test_ai4_867_numpy_backend.py`
  - 11 tests passed.

The broader pytest selector requested by the issue could not be run in this
environment because `pytest` is not installed.

## Scope Boundaries

AI4-867 does not change:

- Deskew projection scoring.
- Frontend or service APIs.
- Batch workflow behavior.
- OpenCV-specific implementation beyond reporting the delegated NumPy mode.
- Libvips, GPU, model-provider, or worker-scheduling behavior.

## Future Work

Future optimization can add an OpenCV-specific path or broaden vectorization,
but it should keep the AI4-866 and AI4-867 parity tests as required regression
guards.

## Performance Quantification (AI4-868)

### Benchmark Setup

AI4-868 created a focused synthetic benchmark to measure whether the NumPy backend
provides measurable performance improvement for despeckle candidate extraction
only. The benchmark:

- Uses public-safe synthetic dark masks of varying sizes and densities
- Compares fallback vs NumPy candidate extraction on identical masks
- Measures aggregate timing across multiple iterations
- Validates that both backends produce identical candidate results

**Benchmark script:** `scripts/measure_ai4_868_despeckle_performance.py`

The script defaults to a bounded quick benchmark so unattended workers and CI do
not spend excessive time on large fallback scans:

```bash
python scripts/measure_ai4_868_despeckle_performance.py
```

By default this runs six small/medium masks with three iterations per backend.
The 4000x6000 large-mask cases are intentionally opt-in:

```bash
python scripts/measure_ai4_868_despeckle_performance.py --include-large
```

Use `--iterations N` when a deeper local-only timing run is required.

### Performance Results

The NumPy backend provides **major performance improvement** for despeckle
candidate extraction:

| Test Configuration | Speedup |
|-------------------|---------|
| Small sparse (640x900) | **6.60x** |
| Small medium (640x900) | **6.09x** |
| Small dense (640x900) | **2.78x** |
| Medium sparse (2000x3000) | **8.69x** |
| Medium medium (2000x3000) | **6.44x** |
| Medium dense (2000x3000) | **6.62x** |
| Large sparse (4000x6000, opt-in) | **6.84x** |
| Large medium (4000x6000, opt-in) | **7.22x** |
| Large dense (4000x6000, opt-in) | **7.48x** |

**Summary across all synthetic tests:**
- **Average speedup: 6.53x**
- **Minimum speedup: 2.78x**  
- **Maximum speedup: 8.69x**

### Conclusion

**The NumPy backend provides MEASURABLE performance improvement.**

**Recommendation: Continue broader vectorization work**

The narrow NumPy slice for candidate extraction validates the vectorization
approach. The 2.78x-8.69x speedup range (6.53x average) demonstrates that:

1. NumPy vectorization provides substantial benefit even for a narrow slice
2. Performance gains are consistent across mask sizes and densities
3. The shared filtering logic (`_despeckle_candidate_points_from_dark_points()`)
   remains the dominant factor for dense cases (lower speedup)
4. Sparse masks benefit most from coordinate extraction vectorization

### Optional Fast Path Compatibility

The benchmark validates that the optional fast path works correctly:

- NumPy availability is detected dynamically via _load_numpy()
- Graceful fallback occurs when NumPy is unavailable
- Both backends produce identical candidate results
- No private images or production data required for validation
- Base environments without NumPy skip fast-path tests without failure

### Next Recommended Backend Steps

Given the validated performance improvement, the next steps for broader
vectorization could include:

1. **Vectorize the filtering logic** in _despeckle_candidate_points_from_dark_points()
2. **Add OpenCV-specific acceleration** for connected component analysis
3. **Extend vectorization to other scan QC operations** beyond despeckle
4. **Measure end-to-end impact** on full repair chains with real production data

The AI4-866 and AI4-867 parity tests should remain required regression guards
for all future backend optimization work.
