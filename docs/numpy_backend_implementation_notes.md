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
