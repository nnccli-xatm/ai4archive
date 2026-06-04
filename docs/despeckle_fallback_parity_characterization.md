# Despeckle Fallback Behavior Parity Characterization (AI4-866)

## Overview

This document characterizes the current fallback despeckle behavior so future
vectorized implementations have explicit parity anchors. The characterization is
based on synthetic tests in
`tests/test_ai4_866_despeckle_fallback_parity.py`.

## Target Functions

The characterization focuses on these fallback functions:

- `_despeckle_candidate_points_fallback(dark_mask)`: main fallback entry point.
- `_despeckle_candidate_points_from_dark_points(dark_points, width, height, left, top)`: core candidate logic.
- `_despeckle_protected_edge_margin(width, height)`: edge protection calculation.
- `_despeckle_candidate_points_with_backend(mask, backend=...)`: backend dispatch and fallback behavior.

## Key Behavioral Characteristics

### 1. Isolated Pixels

Behavior: single isolated dark pixels are candidates if they are not
edge-protected.

Test coverage:

- Single pixel in the image center: candidate.
- Multiple isolated pixels: all candidates.
- Edge-adjacent pixels: excluded by the protected margin.
- Border pixels: excluded at absolute image borders.

Key threshold:

- There is no size limit for isolated pixels as long as edge protection is
  respected.

### 2. Tiny Clusters

Behavior: connected clusters up to the documented limits are candidates if they
meet regular component or short lint streak criteria.

Test coverage:

- 2x2 cluster, 4 pixels: candidate.
- 3x3 cluster, 9 pixels: candidate.
- Linear streak, 3 pixels: candidate.
- Linear streak, 12 pixels: candidate at the short lint streak maximum.
- Diagonal streak, 3 pixels: candidate.

Key thresholds:

- `_DESPECKLE_MAX_COMPONENT_PIXELS = 4`
- `_DESPECKLE_MAX_SHORT_LINT_STREAK_PIXELS = 12`
- `_DESPECKLE_MIN_SHORT_LINT_STREAK_PIXELS = 5`
- `_DESPECKLE_MAX_SHORT_LINT_STREAK_MINOR_SPAN = 2`

Short lint streak criteria:

1. Length is between 5 and 12 pixels.
2. Geometry is linear.
3. Minor span is at most 2 pixels.
4. Orientation is horizontal or vertical.

### 3. Larger Clusters

Behavior: clusters exceeding limits are excluded unless they satisfy short lint
streak criteria.

Test coverage:

- 4x4 cluster, 16 pixels: excluded.
- Horizontal line longer than 12 pixels: excluded.
- Irregular shape over the component limits: excluded.

Key thresholds:

- `_DESPECKLE_MAX_COMPONENT_SPAN = 3`
- `_DESPECKLE_MAX_TINY_DUST_CLUSTER_PIXELS = 9`

### 4. Edge Protection

Behavior: pixels near image edges are protected from despeckling.

Test coverage:

- Dynamic margin calculation follows the size-based formula.
- Pixels inside the protected margin are excluded.
- Pixels outside the protected margin are included if other criteria pass.

Key formula:

```python
_despeckle_protected_edge_margin(width, height) = min(5, max(1, min(width, height) // 12))
```

Edge cases:

- Small images, 50 by 50: margin is 4 pixels.
- Medium images, 120 by 120: margin is 5 pixels.
- Large images, 1000 by 1000: margin is capped at 5 pixels.

### 5. Dense Connected Content

Behavior: dense content triggers prefilter-specific handling.

Key thresholds:

- `_DESPECKLE_DENSE_PREFILTER_MIN_DARK_PIXELS = 512`
- `_DESPECKLE_DENSE_FULL_COMPONENT_MAX_DARK_PIXELS = 50000`

Behavior:

- Below 512 dark pixels: normal processing.
- From 512 through 50000 dark pixels: full component analysis.
- Above 50000 dark pixels: low-connectivity filtering.

Test coverage:

- Below the prefilter threshold: normal processing.
- Sparse scattered pixels: all candidates.
- Empty mask: empty result.
- Tiny images smaller than 3 by 3: empty result.

### 6. Backend Fallback Behavior

Behavior: backend dispatch uses fallback mode explicitly when requested and
rejects invalid backend names.

Test coverage:

- Explicit fallback backend: uses fallback mode.
- Invalid backend: raises `ValueError`.

Backend priority:

1. OpenCV when available and requested.
2. NumPy when available and requested.
3. Fallback, which is always available.

## Deterministic Properties

Sorting:

- Candidate output is sorted by `(y, x)` coordinates.
- Sorting provides reproducible ordering across runs.

Reproducibility:

- The same input produces the same output.
- The fallback path has no randomness or state-dependent behavior.

## Test Statistics

Total tests: 27

- Isolated pixels: 4 tests.
- Tiny clusters: 6 tests.
- Larger clusters: 3 tests.
- Edge protection: 3 tests.
- Direct function behavior: 3 tests.
- Dense content: 4 tests.
- Backend fallback: 2 tests.
- Determinism: 2 tests.

Pass rate: 100% (27 of 27 passing).

## Parity Validation Guidelines

For future vectorized implementations:

1. All 27 synthetic tests must pass.
2. Output ordering must remain deterministic and identical.
3. Edge protection must use the same margin calculation and exclusion behavior.
4. Component filtering must respect all current `_DESPECKLE_*` constants.
5. Existing real-world scan behavior must remain backward compatible.

## Implementation Notes

No production code changes are required for this characterization. The tests
serve as regression guards before future optimization work.

The tests use only synthetic PIL images with programmatically generated patterns.
They do not depend on real image data, filenames, hashes, OCR text, or thumbnails.

Future vectorization work should:

1. Run this test suite first to establish fallback parity.
2. Implement the optimized backend while preserving parity.
3. Keep these tests as regression guards.
4. Add performance benchmarks separately.

## Constants Reference

```python
# Size limits
_DESPECKLE_MAX_COMPONENT_PIXELS = 4
_DESPECKLE_MAX_TINY_DUST_CLUSTER_PIXELS = 9
_DESPECKLE_MAX_SHORT_LINT_STREAK_PIXELS = 12
_DESPECKLE_MIN_SHORT_LINT_STREAK_PIXELS = 5

# Span limits
_DESPECKLE_MAX_COMPONENT_SPAN = 3
_DESPECKLE_MAX_SHORT_LINT_STREAK_MINOR_SPAN = 2

# Dense content thresholds
_DESPECKLE_DENSE_PREFILTER_MIN_DARK_PIXELS = 512
_DESPECKLE_DENSE_FULL_COMPONENT_MAX_DARK_PIXELS = 50000

# Edge protection
# min(5, max(1, min(width, height) // 12))
```

## NumPy Backend Implementation Status (AI4-867)

AI4-867 validates the first narrow NumPy-backed candidate selection slice with
full parity to the fallback behavior described here.

- 11 NumPy backend parity tests pass.
- All 27 AI4-866 fallback parity tests continue to pass.
- Backend dispatch reports `numpy` when the OpenCV option delegates to the NumPy implementation.
- Implementation notes are documented in `docs/numpy_backend_implementation_notes.md`.

The NumPy backend uses `np.nonzero()` for dark pixel extraction and delegates to
`_despeckle_candidate_points_from_dark_points()`, keeping candidate behavior
identical to the fallback path.


## Related Issues

- AI4-865: vectorized despeckle implementation, parked pending this characterization.
- AI4-866: fallback parity characterization.
- AI4-867: NumPy backend slice implementation and validation.
- Future: OpenCV backend and comprehensive performance benchmarking.
