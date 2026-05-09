#!/usr/bin/env python3
"""Read-only dependency and wheelhouse checks for offline validation hosts."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from importlib import metadata
import re
from pathlib import Path
import sys
import tomllib


REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"
SRC_DIR = REPO_ROOT / "src"

IMPORT_NAMES = {
    "ai4archive": "archive_scan_qc",
    "pillow": "PIL",
    "setuptools": "setuptools",
}


@dataclass(frozen=True)
class Requirement:
    name: str
    specifiers: tuple[str, ...]
    import_name: str | None = None
    category: str = "runtime"

    @property
    def normalized_name(self) -> str:
        return _normalize_name(self.name)


def _normalize_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _parse_requirement(value: str) -> Requirement:
    match = re.match(r"\s*([A-Za-z0-9_.-]+)\s*(.*)\s*$", value)
    if not match:
        raise ValueError(f"unsupported requirement format: {value!r}")
    name = match.group(1)
    specifier_text = match.group(2).strip()
    specifiers = tuple(part.strip() for part in specifier_text.split(",") if part.strip())
    return Requirement(name=name, specifiers=specifiers, import_name=IMPORT_NAMES.get(_normalize_name(name)))


def _load_requirements() -> tuple[str, list[Requirement]]:
    with PYPROJECT.open("rb") as handle:
        pyproject = tomllib.load(handle)
    project = pyproject["project"]
    build_system = pyproject["build-system"]
    requires_python = str(project["requires-python"])
    requirements = [_parse_requirement(item) for item in project.get("dependencies", [])]
    requirements.extend(
        Requirement(req.name, req.specifiers, req.import_name, "build")
        for req in (_parse_requirement(item) for item in build_system.get("requires", []))
    )
    requirements.append(Requirement("ai4archive", tuple(), "archive_scan_qc", "project"))
    deduped: dict[str, Requirement] = {}
    for requirement in requirements:
        deduped.setdefault(requirement.normalized_name, requirement)
    return requires_python, list(deduped.values())


def _version_tuple(value: str) -> tuple[int, ...]:
    parts = []
    for part in re.split(r"[.+!-]", value):
        if part.isdigit():
            parts.append(int(part))
        else:
            break
    return tuple(parts)


def _satisfies(version: str, specifiers: tuple[str, ...]) -> bool:
    actual = _version_tuple(version)
    for specifier in specifiers:
        match = re.match(r"(>=|<=|==|>|<)\s*([0-9][A-Za-z0-9.!+_-]*)$", specifier)
        if not match:
            continue
        op, expected_text = match.groups()
        expected = _version_tuple(expected_text)
        if op == ">=" and not actual >= expected:
            return False
        if op == "<=" and not actual <= expected:
            return False
        if op == "==" and not actual == expected:
            return False
        if op == ">" and not actual > expected:
            return False
        if op == "<" and not actual < expected:
            return False
    return True


def _python_satisfies(requires_python: str) -> bool:
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    specifiers = tuple(part.strip() for part in requires_python.split(",") if part.strip())
    return _satisfies(version, specifiers)


def _distribution_version(requirement: Requirement) -> str | None:
    if requirement.normalized_name == "ai4archive" and str(SRC_DIR) not in sys.path:
        sys.path.insert(0, str(SRC_DIR))
    try:
        return metadata.version(requirement.name)
    except metadata.PackageNotFoundError:
        if requirement.normalized_name != "ai4archive":
            return None
        try:
            from archive_scan_qc import __version__
        except Exception:
            return None
        return __version__


def _importable(import_name: str | None) -> bool:
    if not import_name:
        return True
    try:
        __import__(import_name)
    except Exception:
        return False
    return True


def _wheel_counts(wheelhouse: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    if not wheelhouse.is_dir():
        return counts
    for wheel in wheelhouse.glob("*.whl"):
        distribution = wheel.name.split("-", 1)[0]
        normalized = _normalize_name(distribution)
        counts[normalized] = counts.get(normalized, 0) + 1
    return counts


def check_dependencies(
    *,
    wheelhouse: Path | None = None,
    wheelhouse_warning_only: bool = False,
) -> tuple[int, list[str]]:
    requires_python, requirements = _load_requirements()
    lines = [
        "Offline dependency check",
        f"python: {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro} "
        f"required={requires_python} status={'ok' if _python_satisfies(requires_python) else 'missing'}",
    ]
    failed = not _python_satisfies(requires_python)

    for requirement in requirements:
        if requirement.category == "build":
            continue
        version = _distribution_version(requirement)
        import_ok = _importable(requirement.import_name)
        spec_ok = bool(version) and _satisfies(version, requirement.specifiers)
        status = "ok" if version and import_ok and spec_ok else "missing"
        version_text = version if version else "not-installed"
        spec_text = ",".join(requirement.specifiers) if requirement.specifiers else "declared-project"
        import_text = requirement.import_name or "none"
        lines.append(
            f"package: {requirement.normalized_name} category={requirement.category} version={version_text} "
            f"required={spec_text} import={import_text} importable={'yes' if import_ok else 'no'} status={status}"
        )
        if status != "ok":
            failed = True

    if wheelhouse is not None:
        counts = _wheel_counts(wheelhouse)
        if wheelhouse.is_dir():
            lines.append(f"wheelhouse: provided=yes wheel_files={sum(counts.values())}")
        else:
            lines.append("wheelhouse: provided=yes status=missing-directory")
            if not wheelhouse_warning_only:
                failed = True
        for requirement in requirements:
            count = counts.get(requirement.normalized_name, 0)
            status = "ok" if count else "missing"
            lines.append(f"wheelhouse-package: {requirement.normalized_name} wheels={count} status={status}")
            if not count and not wheelhouse_warning_only:
                failed = True
    else:
        lines.append("wheelhouse: provided=no")

    lines.append(f"result: {'fail' if failed else 'pass'}")
    return (1 if failed else 0), lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only offline dependency verification. The command never installs packages "
            "and prints only aggregate package/version/wheel counts."
        )
    )
    parser.add_argument("--wheelhouse", type=Path, default=None, help="Optional local wheelhouse directory to count.")
    parser.add_argument(
        "--wheelhouse-warning-only",
        action="store_true",
        help="Report missing wheelhouse wheels as warnings without failing the command.",
    )
    args = parser.parse_args(argv)
    exit_code, lines = check_dependencies(wheelhouse=args.wheelhouse, wheelhouse_warning_only=args.wheelhouse_warning_only)
    print("\n".join(lines))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
