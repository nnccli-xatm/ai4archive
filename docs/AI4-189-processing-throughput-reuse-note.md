# AI4-189 Processing Throughput Reuse Note

This change keeps default processing quality semantics unchanged. Reuse remains conservative and applies only when the source hash, processing option fingerprint, and derivative output hash are already validated, or when scan measurements are explicitly requested with `reuse_scan_measurements`.

Accepted reuse opportunities:

- Resume skips an existing derivative only when the previous successful record still matches the scan report source hash, the current source file hash, the processing options fingerprint, and the existing derivative hash.
- Duplicate source images reuse the first processed derivative for the same input hash. If the duplicate target already has the expected derivative hash, the copy is skipped.
- Scan skew and dark-border measurements can be reused by processing and processing-plan dry runs when `reuse_scan_measurements` is enabled and the scan record is openable, dimensions match the post-EXIF-transpose processing image, and the relevant measurement is complete. Dark-border reuse is rejected after deskew changes the coordinate space.

Rejected reuse opportunities:

- No default visual-quality changes, threshold changes, or new image algorithms were added.
- No reuse is attempted for missing, partial, dimension-mismatched, EXIF-transposed, failed, stale, or hash-mismatched records.
- No public row-level evidence was added. Aggregate counters and timing summaries remain aggregate-only; sensitive manifests and processing plans still contain local file evidence and are marked non-public.
