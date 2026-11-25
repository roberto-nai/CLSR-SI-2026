"""Export descriptive statistics from verified frozen traces without running models."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from curia_assessment.event_log_statistics import (
    format_event_log_statistics,
    write_event_log_statistics,
)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _object(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _frozen_inputs(study: Path) -> tuple[Path, Path, bytes, bytes, dict[str, str]]:
    candidates = (
        study / "case_assessment/_audit/prepared/pairs.jsonl",
        study / "_audit/prepared/pairs.jsonl",
        study / "prepared/pairs.jsonl",
    )
    pairs = next((path for path in candidates if path.is_file()), None)
    if pairs is None:
        raise ValueError("No frozen prepared/pairs.jsonl was found in the study.")
    root = pairs.parent.parent.resolve()
    manifest = root / "run_manifest.json"
    if not manifest.is_file():
        raise ValueError(f"The frozen run manifest is missing: {manifest}")
    manifest_bytes = manifest.read_bytes()
    document = json.loads(manifest_bytes, object_pairs_hook=_object)
    expected = document.get("prepared_sha256") if isinstance(document, dict) else None
    if not isinstance(expected, dict) or not expected:
        raise ValueError("The frozen manifest has no prepared_sha256 entries.")
    pairs_key = pairs.relative_to(root).as_posix()
    if pairs_key not in expected:
        raise ValueError("The frozen manifest does not protect prepared/pairs.jsonl.")
    pairs_bytes: bytes | None = None
    for relative, digest in expected.items():
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-fA-F]{64}", digest) is None:
            raise ValueError(f"Invalid prepared SHA-256 for {relative}.")
        location = root / relative
        if Path(relative).is_absolute() or not location.resolve().is_relative_to(root):
            raise ValueError(f"The frozen manifest path escapes its directory: {relative}")
        if not location.is_file():
            raise ValueError(f"A protected frozen file is missing: {relative}")
        content = location.read_bytes()
        if _sha256(content) != digest.lower():
            raise ValueError(f"Frozen prepared file SHA-256 mismatch: {relative}")
        if relative == pairs_key:
            pairs_bytes = content
    assert pairs_bytes is not None
    return pairs, manifest, pairs_bytes, manifest_bytes, expected


def export_statistics(study: Path, output_dir: Path | None = None) -> dict[str, Any]:
    """Verify every frozen prepared hash, then write a separate local report."""
    study = Path(study).resolve()
    output = Path(output_dir).resolve() if output_dir is not None else study / "descriptive_statistics"
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError(f"The output destination must be a new or empty directory: {output}")
    pairs, manifest, pairs_bytes, manifest_bytes, expected = _frozen_inputs(study)
    if output.is_relative_to(pairs.parent.resolve()):
        raise ValueError("Descriptive reports must be written outside the frozen prepared directory.")
    records = [json.loads(line, object_pairs_hook=_object)
               for line in pairs_bytes.decode("utf-8-sig").splitlines() if line.strip()]
    if any(not isinstance(record, dict) for record in records):
        raise ValueError("Each frozen pair must be a JSON object.")
    stats = write_event_log_statistics(output, records)
    provenance = {
        "schema_version": 1,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "source_kind": "verified_frozen_prepared_pairs",
        "study_directory": str(study),
        "pairs_path": pairs.relative_to(study).as_posix(),
        "pairs_sha256": _sha256(pairs_bytes),
        "manifest_path": manifest.relative_to(study).as_posix(),
        "manifest_sha256": _sha256(manifest_bytes),
        "verified_prepared_files": len(expected),
        "verification": "Every prepared_sha256 entry was verified against its frozen file.",
        "model_requests_issued": 0,
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "event_log_statistics_provenance.json").write_text(
        json.dumps(provenance, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study", type=Path, required=True, help="Frozen study directory.")
    parser.add_argument("--output-dir", type=Path,
                        help="New or empty destination (default: STUDY/descriptive_statistics).")
    arguments = parser.parse_args(argv)
    try:
        stats = export_statistics(arguments.study, arguments.output_dir)
    except (OSError, ValueError, TypeError) as error:
        parser.error(str(error))
    print(format_event_log_statistics(stats))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
