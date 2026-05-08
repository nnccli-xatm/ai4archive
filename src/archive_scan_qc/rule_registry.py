"""Privacy-safe registry for scan QC finding rules."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import Any


@dataclass(frozen=True)
class RuleMetadata:
    rule_id: str
    title: str
    default_severity: str
    standards: tuple[str, ...]
    check_target: str
    automation_status: str
    report_explanation: str

    def to_report_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["standards"] = list(self.standards)
        return payload


RULE_REGISTRY: dict[str, RuleMetadata] = {
    "openability": RuleMetadata(
        "openability",
        "Image openability",
        "P0",
        ("DA/T 31-2017 11.2 catalog-image count and openability", "DA/T 31-2017 12.2 acceptance scope"),
        "source image file",
        "automated",
        "The image library could not open or validate the file, so the page cannot be accepted without repair or replacement.",
    ),
    "unsupported_format": RuleMetadata(
        "unsupported_format",
        "Supported image format",
        "P1",
        ("DA/T 31-2017 9.5 scan parameters and image format", "DA/T 31-2017 12.2 acceptance scope"),
        "source image file extension",
        "automated",
        "The file extension is outside the image formats supported by the current production scanner.",
    ),
    "dpi_missing": RuleMetadata(
        "dpi_missing",
        "DPI metadata present",
        "P1",
        ("DA/T 31-2017 9.5 scan parameters and image format",),
        "image metadata",
        "automated",
        "The image does not expose both horizontal and vertical DPI metadata needed for scan-parameter evidence.",
    ),
    "dpi_minimum": RuleMetadata(
        "dpi_minimum",
        "Minimum scan resolution",
        "P0",
        ("DA/T 31-2017 9.5 scan parameters and image format",),
        "image DPI metadata",
        "automated",
        "One or both DPI values are below the active rules profile minimum for the batch.",
    ),
    "dimensions": RuleMetadata(
        "dimensions",
        "Image dimensions present",
        "P0",
        ("DA/T 31-2017 10.5.1 complete and readable image",),
        "image metadata",
        "automated",
        "The image has missing or invalid width or height metadata and cannot be reliably reviewed.",
    ),
    "multi_page_image_container": RuleMetadata(
        "multi_page_image_container",
        "Multi-page image container",
        "P2",
        ("DA/T 31-2017 10.5.2 missed/rescanned/extra scans", "DA/T 31-2017 12.2 acceptance scope"),
        "image container frame/page metadata",
        "automated screening",
        "The image container exposes more than one frame/page and should be reviewed against the project's single-page delivery policy.",
    ),
    "duplicate_name": RuleMetadata(
        "duplicate_name",
        "Duplicate filename in directory",
        "P0",
        ("DA/T 31-2017 11.2 catalog-image correspondence", "DA/T 31-2017 10.5.3 page order consistency"),
        "batch file inventory",
        "automated",
        "More than one file in the same directory resolves to the same case-insensitive filename.",
    ),
    "duplicate_file": RuleMetadata(
        "duplicate_file",
        "Duplicate file content",
        "P0",
        ("DA/T 31-2017 10.5.2 missed/rescanned/extra scans", "DA/T 31-2017 12.2 acceptance scope"),
        "batch file hashes",
        "automated",
        "Two or more scanned files have identical SHA-256 content hashes and should be reviewed for repeated pages.",
    ),
    "manifest_missing_file": RuleMetadata(
        "manifest_missing_file",
        "Manifest file missing from batch",
        "P0",
        ("DA/T 31-2017 11.2 catalog-image correspondence", "DA/T 31-2017 10.5.2 missed/rescanned/extra scans"),
        "manifest relative_path",
        "automated",
        "The manifest expects a relative path that was not present in the scanned input directory.",
    ),
    "manifest_unexpected_file": RuleMetadata(
        "manifest_unexpected_file",
        "Unexpected file not in manifest",
        "P0",
        ("DA/T 31-2017 11.2 catalog-image correspondence", "DA/T 31-2017 10.5.2 missed/rescanned/extra scans"),
        "batch file inventory",
        "automated",
        "The scanned input directory contains a file that is not listed in the manifest.",
    ),
    "manifest_duplicate_entry": RuleMetadata(
        "manifest_duplicate_entry",
        "Duplicate manifest entry",
        "P0",
        ("DA/T 31-2017 11.2 catalog-image correspondence", "DA/T 31-2017 10.5.3 page order consistency"),
        "manifest relative_path",
        "automated",
        "The manifest repeats the same relative path and should be corrected before acceptance.",
    ),
    "manifest_invalid_sequence": RuleMetadata(
        "manifest_invalid_sequence",
        "Invalid manifest page sequence",
        "P1",
        ("DA/T 31-2017 10.5.3 page order consistency", "DA/T 31-2017 11.2 catalog-image correspondence"),
        "manifest sequence field",
        "automated",
        "A manifest page/order value is present but is not a positive integer, so page order evidence is ambiguous.",
    ),
    "manifest_duplicate_sequence": RuleMetadata(
        "manifest_duplicate_sequence",
        "Duplicate manifest page sequence",
        "P0",
        ("DA/T 31-2017 10.5.3 page order consistency", "DA/T 31-2017 11.2 catalog-image correspondence"),
        "manifest sequence field",
        "automated",
        "Two or more manifest rows declare the same page/order value and require catalog-image order correction.",
    ),
    "manifest_sequence_gap": RuleMetadata(
        "manifest_sequence_gap",
        "Strict manifest page sequence gap",
        "P1",
        ("DA/T 31-2017 10.5.3 page order consistency", "DA/T 31-2017 10.5.2 missed/rescanned/extra scans"),
        "manifest sequence field",
        "automated",
        "The manifest declares strict sequence mode but the numeric page/order values are not contiguous.",
    ),
    "manifest_order_mismatch": RuleMetadata(
        "manifest_order_mismatch",
        "Manifest order differs from discovered file order",
        "P2",
        ("DA/T 31-2017 10.5.3 page order consistency", "DA/T 31-2017 11.2 catalog-image correspondence"),
        "manifest row order and discovered file order",
        "automated",
        "The manifest row order does not match the scanner's deterministic discovered file order for the same files.",
    ),
    "name_pattern": RuleMetadata(
        "name_pattern",
        "Configured filename pattern",
        "P1",
        ("DA/T 31-2017 11.2 catalog-image correspondence", "DA/T 31-2017 12.2 acceptance scope"),
        "filename stem",
        "automated",
        "The filename stem does not match the active rules profile naming pattern for the batch.",
    ),
    "quality_too_dark": RuleMetadata(
        "quality_too_dark",
        "Image too dark",
        "P1",
        ("DA/T 31-2017 10.5.1 complete and readable image", "DA/T 31-2017 10.4 decontaminate while preserving original appearance"),
        "thumbnail luminance metrics",
        "automated screening",
        "Mean grayscale brightness is below the configured conservative threshold and needs visual review.",
    ),
    "quality_too_bright": RuleMetadata(
        "quality_too_bright",
        "Image too bright",
        "P1",
        ("DA/T 31-2017 10.5.1 complete and readable image", "DA/T 31-2017 10.4 decontaminate while preserving original appearance"),
        "thumbnail luminance and contrast metrics",
        "automated screening",
        "The page is very bright with low contrast, which may indicate washed-out capture or over-processing.",
    ),
    "quality_low_contrast": RuleMetadata(
        "quality_low_contrast",
        "Low image contrast",
        "P2",
        ("DA/T 31-2017 10.5.1 complete and readable image",),
        "thumbnail contrast metrics",
        "automated screening",
        "Grayscale standard deviation is below the configured threshold and may reduce readability.",
    ),
    "quality_suspected_blur": RuleMetadata(
        "quality_suspected_blur",
        "Suspected blur",
        "P2",
        ("DA/T 31-2017 10.5.1 complete and readable image",),
        "thumbnail sharpness metrics",
        "automated screening",
        "Laplacian-variance sharpness is below the configured threshold for a page with enough contrast to assess focus.",
    ),
    "quality_near_blank_page": RuleMetadata(
        "quality_near_blank_page",
        "Near blank page",
        "P2",
        ("DA/T 31-2017 10.5.1 complete and readable image", "DA/T 31-2017 10.5.2 missed/rescanned/extra scans"),
        "thumbnail content metrics",
        "automated screening",
        "Very bright, very low-content metrics indicate a possible blank page, separator, or missed scan.",
    ),
    "quality_skew_candidate": RuleMetadata(
        "quality_skew_candidate",
        "Page skew candidate",
        "P2",
        ("DA/T 31-2017 10.2 rotation and deskew", "DA/T 31-2017 10.5.4 processing quality inspection"),
        "source image foreground projection",
        "automated screening",
        "A conservative scan-time skew estimate indicates the page may need deskew review before derivative approval.",
    ),
    "quality_dark_border_candidate": RuleMetadata(
        "quality_dark_border_candidate",
        "Dark scan border candidate",
        "P2",
        ("DA/T 31-2017 10.3 border cropping", "DA/T 31-2017 10.5.4 processing quality inspection"),
        "source image edge luminance",
        "automated screening",
        "Dark pixels touching the outer image edges indicate a likely scanner border that should be reviewed for trim.",
    ),
    "quality_scanline_candidate": RuleMetadata(
        "quality_scanline_candidate",
        "Scanline or streak artifact candidate",
        "P2",
        ("DA/T 31-2017 10.4 decontaminate while preserving original appearance", "DA/T 31-2017 10.5.1 complete and readable image"),
        "thumbnail row and column intensity anomalies",
        "automated screening",
        "A conservative full-span row or column anomaly indicates a possible scanner line or streak artifact.",
    ),
    "batch_format_consistency": RuleMetadata(
        "batch_format_consistency",
        "Batch format consistency",
        "P2",
        ("DA/T 31-2017 9.5 scan parameters and image format", "DA/T 31-2017 12.2 acceptance scope"),
        "openable batch image metadata",
        "automated",
        "Openable files in the batch use multiple image formats and should be reviewed against delivery rules.",
    ),
    "batch_color_mode_consistency": RuleMetadata(
        "batch_color_mode_consistency",
        "Batch color mode consistency",
        "P2",
        ("DA/T 31-2017 9.5 scan parameters and image format", "DA/T 31-2017 10.4 decontaminate while preserving original appearance"),
        "openable batch image metadata",
        "automated",
        "Openable files in the batch use multiple color modes and should be reviewed for expected capture settings.",
    ),
    "batch_dpi_consistency": RuleMetadata(
        "batch_dpi_consistency",
        "Batch DPI consistency",
        "P2",
        ("DA/T 31-2017 9.5 scan parameters and image format",),
        "openable batch image metadata",
        "automated",
        "Openable files in the batch use multiple DPI pairs and should be checked against the batch rules profile.",
    ),
    "batch_orientation_consistency": RuleMetadata(
        "batch_orientation_consistency",
        "Batch orientation consistency",
        "P2",
        ("DA/T 31-2017 10.2 rotation and deskew", "DA/T 31-2017 10.5.4 processing quality inspection"),
        "openable batch image geometry",
        "automated screening",
        "The batch mixes portrait and landscape page geometry at a material ratio, which may indicate rotated pages or mixed attachments.",
    ),
}

PROVIDER_RULE_PATTERN = re.compile(r"^provider\.[a-z0-9][a-z0-9_-]*\.[a-z0-9][a-z0-9_.-]*$")

PROVIDER_RULE_POLICY = {
    "namespace": "provider.<name>.<rule>",
    "constraints": [
        "Provider rule ids must use the provider namespace and must not reuse built-in rule ids.",
        "Provider findings are external local analysis signals and cannot override protected built-in P0 rules.",
        "Provider metadata must be aggregate or model/run metadata only; reports must not include images, thumbnails, OCR text, or file content.",
    ],
    "protected_builtin_p0_rules": [
        "openability",
        "dpi_minimum",
        "dimensions",
        "duplicate_name",
        "duplicate_file",
        "manifest_missing_file",
        "manifest_unexpected_file",
        "manifest_duplicate_entry",
    ],
}


def rule_catalog() -> dict[str, dict[str, Any]]:
    return {rule_id: metadata.to_report_dict() for rule_id, metadata in sorted(RULE_REGISTRY.items())}


def validate_provider_rule_id(rule_id: str) -> None:
    if rule_id in RULE_REGISTRY:
        raise ValueError(f"provider rule id '{rule_id}' cannot override a built-in rule.")
    if not PROVIDER_RULE_PATTERN.fullmatch(rule_id):
        raise ValueError("provider rule ids must match provider.<name>.<rule> using lowercase letters, numbers, _, -, or dots.")
