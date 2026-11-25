"""Exact activity vocabularies and deterministic argument membership checks.

These checks do not decide whether a DECLARE template expresses a legally
sound requirement. They prevent activity labels outside the selected log
from being treated as eligible model output.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from pathlib import Path
from typing import Any


def read_activity_vocabulary(path: Path, column: str = "Activity") -> dict[str, Any]:
    """Derive exact labels from a comma-, semicolon- or tab-separated log.

    No case folding, trimming, synonym replacement or Unicode normalisation
    is performed. Empty or malformed values stop preparation rather than
    silently removing observations. Only vocabulary and provenance metadata
    are returned; case-level rows are never returned to the caller.
    """
    if not isinstance(column, str) or not column.strip():
        raise ValueError("activity_column must name a non-empty CSV column.")
    if not path.is_file():
        raise ValueError(f"The event log is missing: {path}. Supply the CSV before creating a plan.")
    original = path.read_bytes()
    try:
        text = original.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("The event log must be encoded as UTF-8, optionally with a BOM.") from exc
    if not text.strip():
        raise ValueError("The event log is empty.")

    candidates = []
    for delimiter in (",", ";", "\t"):
        try:
            fields = next(csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True))
        except (csv.Error, StopIteration):
            continue
        if column in fields:
            candidates.append((delimiter, fields))
    if not candidates:
        raise ValueError(f"The event log has no exact {column!r} column in a supported CSV format.")
    # A genuine single-column CSV has the same header for every delimiter.
    if len(candidates) > 1 and not all(fields == [column] for _, fields in candidates):
        raise ValueError("The CSV delimiter is ambiguous; use an unambiguous comma- or semicolon-separated header.")
    delimiter, fields = candidates[0]
    if len(fields) != len(set(fields)):
        raise ValueError("The event log has duplicate column headings.")

    activities: set[str] = set()
    rows = 0
    reader = csv.DictReader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
    try:
        for row in reader:
            rows += 1
            if None in row:
                raise ValueError(f"The event log has extra CSV fields on line {reader.line_num}.")
            activity = row.get(column)
            if not isinstance(activity, str) or not activity.strip():
                raise ValueError(f"The event log has a missing or blank activity on line {reader.line_num}.")
            if any(ord(character) < 32 for character in activity):
                raise ValueError(f"The activity on line {reader.line_num} contains a control character.")
            activities.add(activity)
    except csv.Error as exc:
        raise ValueError(f"Malformed CSV near line {reader.line_num}.") from exc
    if not activities:
        raise ValueError("The event log contains no activity observations.")
    return {
        "schema_version": 1,
        "source_file": path.name,
        "source_sha256": hashlib.sha256(original).hexdigest(),
        "encoding": "utf-8-sig",
        "delimiter": delimiter,
        "activity_column": column,
        "row_count": rows,
        "activity_count": len(activities),
        "activities": sorted(activities),
    }


def validate_declare_activities(formula: str, activities: list[str]) -> dict[str, Any]:
    """Check quoted activity arguments without assessing template semantics.

    The declared notation is Template("Exact label") or
    Template("Exact label A", "Exact label B"). JSON escaping is permitted
    within labels. Template membership, arity for a particular template and
    normative meaning remain matters for the existing human assessment.
    """
    result: dict[str, Any] = {
        "activity_vocabulary_valid": False,
        "activity_validation_status": "invalid_argument_notation",
        "activity_validation_issues": [],
        "activity_arguments": [],
    }
    match = re.fullmatch(r"\s*[A-Za-z][A-Za-z0-9_]*\s*\((.*)\)\s*", formula, re.DOTALL) if isinstance(formula, str) else None
    if match is None:
        result["activity_validation_issues"].append("Expected one Template(...) expression with JSON-quoted activity arguments.")
        return result
    try:
        arguments = json.loads("[" + match.group(1) + "]")
    except (json.JSONDecodeError, TypeError):
        result["activity_validation_issues"].append("Activity arguments must be JSON-quoted strings; bare names or additional expressions are not allowed.")
        return result
    if len(arguments) not in (1, 2) or not all(isinstance(argument, str) for argument in arguments):
        result["activity_validation_issues"].append("Expected one or two JSON-quoted activity labels.")
        return result
    result["activity_arguments"] = arguments
    allowed = set(activities)
    unknown = list(dict.fromkeys(argument for argument in arguments if argument not in allowed))
    if unknown:
        result["activity_validation_status"] = "unknown_activity"
        result["activity_validation_issues"].append("Activity labels absent from the frozen event-log vocabulary: " + json.dumps(unknown, ensure_ascii=False))
        return result
    result["activity_vocabulary_valid"] = True
    result["activity_validation_status"] = "valid"
    return result
