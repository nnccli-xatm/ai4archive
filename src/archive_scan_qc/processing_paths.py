"""Processing path registry.

The public API chooses rule templates. Internally, each template resolves to a
stable processing path so independent algorithms can be compared without
changing CLI or service endpoint shapes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class ProcessingPathSpec:
    path_id: str
    family: str
    implementation: str
    output_profile: str
    description: str
    independent_route: bool

    def public_payload(self) -> dict[str, Any]:
        return asdict(self)


PROCESSING_PATHS: dict[str, ProcessingPathSpec] = {
    "standard-conservative-v1": ProcessingPathSpec(
        path_id="standard-conservative-v1",
        family="archival",
        implementation="archive_scan_qc.processing.standard_conservative",
        output_profile="standard",
        description="Conservative archival derivative pipeline.",
        independent_route=False,
    ),
    "print-clean-v1": ProcessingPathSpec(
        path_id="print-clean-v1",
        family="text-cleanup",
        implementation="archive_scan_qc.processing.print_clean",
        output_profile="print_clean",
        description="Text-oriented cleanup path with stronger readability defaults.",
        independent_route=False,
    ),
    "ocr-preprocess-light-v1": ProcessingPathSpec(
        path_id="ocr-preprocess-light-v1",
        family="ocr-preprocessing",
        implementation="archive_scan_qc.processing.ocr_preprocess_light",
        output_profile="ocr_preprocess_light",
        description="Light OCR preprocessing path.",
        independent_route=False,
    ),
    "ocr-preprocess-v1": ProcessingPathSpec(
        path_id="ocr-preprocess-v1",
        family="ocr-preprocessing",
        implementation="archive_scan_qc.processing.ocr_preprocess_strong",
        output_profile="ocr_preprocess",
        description="Strong OCR preprocessing path.",
        independent_route=False,
    ),
    "ocr-preprocess-leptonica-v1": ProcessingPathSpec(
        path_id="ocr-preprocess-leptonica-v1",
        family="ocr-preprocessing",
        implementation="archive_scan_qc.processing.ocr_preprocess_leptonica",
        output_profile="ocr_preprocess_leptonica",
        description="Leptonica-style OCR preprocessing path with preserve-canvas deskew.",
        independent_route=True,
    ),
}

PROCESSING_PROFILE_TO_PATH_ID: dict[str, str] = {
    "standard": "standard-conservative-v1",
    "print_clean": "print-clean-v1",
    "ocr_preprocess_light": "ocr-preprocess-light-v1",
    "ocr_preprocess": "ocr-preprocess-v1",
    "ocr_preprocess_leptonica": "ocr-preprocess-leptonica-v1",
}


def processing_path_id_for_profile(processing_profile: str | None) -> str:
    profile = processing_profile or "standard"
    return PROCESSING_PROFILE_TO_PATH_ID.get(profile, "standard-conservative-v1")


def processing_path_for_profile(processing_profile: str | None) -> ProcessingPathSpec:
    return PROCESSING_PATHS[processing_path_id_for_profile(processing_profile)]


def processing_path_payload_for_profile(processing_profile: str | None) -> dict[str, Any]:
    return processing_path_for_profile(processing_profile).public_payload()
