"""Manifest CSV parsing and page sequence validation."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any


SEQUENCE_FIELDS = ("sequence", "page_sequence", "page_number", "expected_order")
STRICT_SEQUENCE_FIELDS = ("strict_sequence", "sequence_strict", "strict_page_sequence")


@dataclass(frozen=True)
class ManifestEntry:
    relative_path: str
    row_number: int
    order_index: int
    sequence_raw: str | None
    sequence: int | None


@dataclass(frozen=True)
class ManifestValidation:
    used: bool
    readable: bool
    has_relative_path_column: bool
    path: str | None
    sequence_field: str | None
    strict_sequence: bool
    entries: tuple[ManifestEntry, ...]
    entry_count: int
    unique_entry_count: int
    empty_path_count: int
    absolute_path_count: int
    parent_escape_count: int
    duplicate_count: int
    missing_count: int
    unexpected_count: int
    sequence_entry_count: int
    sequence_invalid_count: int
    sequence_duplicate_count: int
    sequence_gap_count: int
    sequence_order_mismatch_count: int


def read_manifest(manifest_csv: Path | None, input_paths: set[str] | None = None, file_order: list[str] | None = None) -> ManifestValidation:
    if manifest_csv is None:
        return _empty_manifest(False)

    input_paths = input_paths or set()
    file_order = file_order or []
    try:
        with manifest_csv.open("r", newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            fieldnames = reader.fieldnames or []
            if "relative_path" not in fieldnames:
                return _empty_manifest(
                    True,
                    readable=True,
                    has_relative_path_column=False,
                    path=str(manifest_csv.resolve()),
                    unexpected_count=len(input_paths),
                )
            sequence_field = next((field for field in SEQUENCE_FIELDS if field in fieldnames), None)
            strict_fields = [field for field in STRICT_SEQUENCE_FIELDS if field in fieldnames]
            entries: list[ManifestEntry] = []
            empty = 0
            absolute = 0
            parent_escape = 0
            invalid_sequence = 0
            strict_sequence = False
            for row_number, row in enumerate(reader, start=2):
                raw = row.get("relative_path", "")
                normalized = raw.strip().replace("\\", "/")
                if any(_truthy(row.get(field)) for field in strict_fields):
                    strict_sequence = True
                if not normalized:
                    empty += 1
                    continue
                if _is_absolute_manifest_path(raw, normalized):
                    absolute += 1
                    continue
                parts = PurePosixPath(normalized).parts
                if ".." in parts:
                    parent_escape += 1
                    continue

                sequence_raw = row.get(sequence_field, "") if sequence_field else None
                sequence = _parse_sequence(sequence_raw) if sequence_field else None
                if sequence_field and sequence_raw is not None and sequence_raw.strip() and sequence is None:
                    invalid_sequence += 1
                entries.append(
                    ManifestEntry(
                        relative_path=normalized,
                        row_number=row_number,
                        order_index=len(entries) + 1,
                        sequence_raw=sequence_raw.strip() if sequence_raw is not None else None,
                        sequence=sequence,
                    )
                )
    except OSError:
        return _empty_manifest(True, readable=False, path=str(manifest_csv.resolve()), unexpected_count=len(input_paths))

    paths = [entry.relative_path for entry in entries]
    manifest_set = set(paths)
    path_counts = _counts(paths)
    sequences = [entry.sequence for entry in entries if entry.sequence is not None]
    sequence_counts = _counts(sequences)
    sequence_duplicate_count = sum(1 for count in sequence_counts.values() if count > 1)
    sequence_gap_count = _gap_count(sequences) if strict_sequence else 0
    sequence_order_mismatch_count = _sequence_order_mismatch_count(entries, file_order)
    return ManifestValidation(
        used=True,
        readable=True,
        has_relative_path_column=True,
        path=str(manifest_csv.resolve()),
        sequence_field=sequence_field,
        strict_sequence=strict_sequence,
        entries=tuple(entries),
        entry_count=len(entries) + empty + absolute + parent_escape,
        unique_entry_count=len(manifest_set),
        empty_path_count=empty,
        absolute_path_count=absolute,
        parent_escape_count=parent_escape,
        duplicate_count=sum(1 for count in path_counts.values() if count > 1),
        missing_count=len(manifest_set - input_paths),
        unexpected_count=len(input_paths - manifest_set),
        sequence_entry_count=len(sequences),
        sequence_invalid_count=invalid_sequence,
        sequence_duplicate_count=sequence_duplicate_count,
        sequence_gap_count=sequence_gap_count,
        sequence_order_mismatch_count=sequence_order_mismatch_count,
    )


def manifest_summary(manifest: ManifestValidation) -> dict[str, Any]:
    return {
        "used": manifest.used,
        "readable": manifest.readable,
        "has_relative_path_column": manifest.has_relative_path_column,
        "entry_count": manifest.entry_count,
        "unique_entry_count": manifest.unique_entry_count,
        "empty_path_count": manifest.empty_path_count,
        "absolute_path_count": manifest.absolute_path_count,
        "parent_escape_count": manifest.parent_escape_count,
        "duplicate_count": manifest.duplicate_count,
        "missing_count": manifest.missing_count,
        "unexpected_count": manifest.unexpected_count,
        "sequence_field": manifest.sequence_field,
        "strict_sequence": manifest.strict_sequence,
        "sequence_entry_count": manifest.sequence_entry_count,
        "sequence_invalid_count": manifest.sequence_invalid_count,
        "sequence_duplicate_count": manifest.sequence_duplicate_count,
        "sequence_gap_count": manifest.sequence_gap_count,
        "sequence_order_mismatch_count": manifest.sequence_order_mismatch_count,
    }


def _empty_manifest(
    used: bool,
    *,
    readable: bool = False,
    has_relative_path_column: bool = False,
    path: str | None = None,
    unexpected_count: int = 0,
) -> ManifestValidation:
    return ManifestValidation(
        used=used,
        readable=readable,
        has_relative_path_column=has_relative_path_column,
        path=path,
        sequence_field=None,
        strict_sequence=False,
        entries=(),
        entry_count=0,
        unique_entry_count=0,
        empty_path_count=0,
        absolute_path_count=0,
        parent_escape_count=0,
        duplicate_count=0,
        missing_count=0,
        unexpected_count=unexpected_count,
        sequence_entry_count=0,
        sequence_invalid_count=0,
        sequence_duplicate_count=0,
        sequence_gap_count=0,
        sequence_order_mismatch_count=0,
    )


def _parse_sequence(value: str | None) -> int | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if not text.isdecimal():
        return None
    sequence = int(text)
    return sequence if sequence > 0 else None


def _sequence_order_mismatch_count(entries: list[ManifestEntry], file_order: list[str]) -> int:
    if not file_order:
        return 0
    file_positions = {path: index for index, path in enumerate(file_order)}
    ordered_entries = [entry for entry in entries if entry.relative_path in file_positions]
    if len(ordered_entries) < 2:
        return 0
    expected = [entry.relative_path for entry in ordered_entries]
    actual = [entry.relative_path for entry in sorted(ordered_entries, key=lambda entry: file_positions[entry.relative_path])]
    return sum(1 for left, right in zip(expected, actual, strict=True) if left != right)


def _gap_count(sequences: list[int]) -> int:
    if not sequences:
        return 0
    unique = set(sequences)
    return sum(1 for value in range(min(unique), max(unique) + 1) if value not in unique)


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes", "y", "strict"}


def _counts(values: list[Any]) -> dict[Any, int]:
    counts: dict[Any, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts


def _is_absolute_manifest_path(raw: str, normalized: str) -> bool:
    stripped = raw.strip()
    return normalized.startswith("/") or Path(stripped).is_absolute() or PureWindowsPath(stripped).is_absolute()
