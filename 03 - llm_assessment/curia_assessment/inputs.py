"""Read textual requirements and recorded cases without changing their meaning."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import random
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .case_documents import case_document_prefix, index_case_documents

_VARIANT_TAG = re.compile(r"(?m)^\[(V[0-9]+)\]\r?$")
_EVENT_FIELDS = {"Activity", "Timestamp", "Activity_Party", "ECLI", "Date_Quality"}
_REQUIRED_LOG = {"CaseID", "Activity", "Timestamp"}
_CASE_YEAR = re.compile(r"/([0-9]{4}|[0-9]{2})(?=$|[\s(])")


def validate_year_range(year_start: int, year_end: int) -> None:
    """Require an inclusive range of calendar years."""
    for name, value in (("year_start", year_start), ("year_end", year_end)):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 9999:
            raise ValueError(f"{name} must be an integer between 1 and 9999.")
    if year_start > year_end:
        raise ValueError("year_start must be less than or equal to year_end.")


def case_year(case_id: str) -> int:
    """Read the filing year in CaseID, including suffixes such as P and PPU.

    Two-digit years identify 2000–2099; four-digit years are used as recorded.
    Event timestamps are not a substitute for the case's filing year.
    """
    match = _CASE_YEAR.search(case_id) if isinstance(case_id, str) else None
    if match is None:
        raise ValueError(f"Cannot determine the year from CaseID {case_id!r}; expected /YY or /YYYY.")
    value = match.group(1)
    year = int(value) + (2000 if len(value) == 2 else 0)
    if year == 0:
        raise ValueError(f"Invalid year in CaseID {case_id!r}.")
    return year


def sha256_file(path: str | Path) -> str:
    """Identify the exact input bytes, independently of parsed cell values."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_csv_text(text: str, required: Iterable[str]) -> tuple[list[str], list[dict[str, str]]]:
    """Read comma-, semicolon- or tab-delimited tables from their header."""
    text = text.lstrip("\ufeff")
    required = set(required)
    first_line = text.splitlines()[0] if text else ""
    possibilities = []
    for delimiter in (",", ";", "\t"):
        header = next(csv.reader([first_line], delimiter=delimiter), [])
        if required.issubset(header):
            possibilities.append((delimiter, header))
    if len(possibilities) != 1:
        raise ValueError("Cannot identify a unique CSV delimiter and the required columns.")
    delimiter, header = possibilities[0]
    if len(header) != len(set(header)):
        raise ValueError("Duplicate CSV column names are not supported.")
    reader = csv.DictReader(io.StringIO(text, newline=""), delimiter=delimiter)
    rows = []
    for position, row in enumerate(reader, 2):
        if None in row or any(value is None for value in row.values()):
            raise ValueError(f"Malformed CSV record {position}.")
        if not any(row.values()):
            raise ValueError(f"Empty CSV record {position}.")
        rows.append(row)
    if not rows:
        raise ValueError("The CSV contains no data records.")
    return header, rows


def _variant_blocks(value: str, field: str, rule_id: str) -> list[tuple[str, str]]:
    """Recover aligned review-tagged blocks, preserving their quoted text."""
    tags = list(_VARIANT_TAG.finditer(value))
    if not tags:
        if not value.strip():
            raise ValueError(f"{rule_id}: {field} is empty.")
        return [("", value)]
    if value[:tags[0].start()].strip():
        raise ValueError(f"{rule_id}: unlabelled text precedes the first {field} variant.")
    blocks = []
    for index, tag in enumerate(tags):
        end = tags[index + 1].start() if index + 1 < len(tags) else len(value)
        body = value[tag.end():end]
        if body.startswith("\r\n"):
            body = body[2:]
        elif body.startswith("\n"):
            body = body[1:]
        if index + 1 < len(tags):
            if body.endswith("\r\n\r\n"):
                body = body[:-4]
            elif body.endswith("\n\n"):
                body = body[:-2]
            else:
                raise ValueError(f"{rule_id}: variant blocks must be separated by a blank line.")
        if not body.strip():
            raise ValueError(f"{rule_id}: empty {field} variant {tag.group(1)}.")
        blocks.append((tag.group(1), body))
    ids = [identifier for identifier, _ in blocks]
    if len(ids) != len(set(ids)):
        raise ValueError(f"{rule_id}: duplicate review identifiers in {field}.")
    return blocks


def requirement_only(record: dict[str, Any]) -> dict[str, Any]:
    """Project a rule/pair without the catalogue's unused legal-source text.

    The old review identifier is metadata only; retain it under its current
    requirement-specific name when importing a historical cohort.
    """
    excluded = {"source", "legal_source", "quoted_source", "source_quote", "legal_basis", "source_review_id"}
    copied = {key: value for key, value in record.items() if key not in excluded}
    if "source_review_id" in record and "requirement_review_id" not in copied:
        copied["requirement_review_id"] = record["source_review_id"]
    return copied


def load_catalogue(path: str | Path, variant_ids: list[str] | None = None) -> list[dict[str, Any]]:
    """Expand distinct textual formulations without merging their legal scopes.

    A formal rule with several [V...] requirement blocks gives separate
    variant IDs. A rule with one untagged formulation keeps its original ID.
    The DECLARE string is retained for the formal bridge, not for the LLM prompt.
    The optional source column is archival only and is never parsed or returned.
    """
    _, rows = read_csv_text(Path(path).read_text(encoding="utf-8-sig"),
                            {"ID", "DECLARE", "requirement"})
    base_ids = [row["ID"] for row in rows]
    if any(not value or value != value.strip() or any(c.isspace() for c in value) for value in base_ids):
        raise ValueError("Rule identifiers must be non-empty and contain no whitespace.")
    if len(base_ids) != len(set(base_ids)):
        raise ValueError("The catalogue contains duplicate base rule identifiers.")
    variants = []
    for row in rows:
        if not row["DECLARE"].strip():
            raise ValueError(f"{row['ID']}: DECLARE representation is empty.")
        requirements = _variant_blocks(row["requirement"], "requirement", row["ID"])
        for review_id, requirement in requirements:
            variant_id = row["ID"] + ("__" + review_id if review_id else "")
            variants.append({
                "base_rule_id": row["ID"],
                "variant_id": variant_id,
                "requirement_review_id": review_id,
                "requirement": requirement,
                "declare": row["DECLARE"],
            })
    identifiers = [variant["variant_id"] for variant in variants]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("Expanded variant identifiers are not unique.")
    if variant_ids is not None:
        if not variant_ids or len(variant_ids) != len(set(variant_ids)):
            raise ValueError("Explicit variant selection must be non-empty and contain no duplicates.")
        unknown = set(variant_ids) - set(identifiers)
        if unknown:
            raise ValueError("Unknown variant identifiers: " + ", ".join(sorted(unknown)))
        lookup = {item["variant_id"]: item for item in variants}
        variants = [lookup[identifier] for identifier in variant_ids]
    return variants


def normalised_timestamp(value: str) -> str:
    """Use UTC for ordering only; retain the original timestamp in the dossier."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Invalid recorded timestamp: {value!r}.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def trace_hash(case_id: str, events: list[dict[str, Any]]) -> str:
    """Hash the ordered trace; absolute source row positions are not its content."""
    payload = {
        "case_id": case_id,
        "events": [
            {"activity": event["activity"], "timestamp": normalised_timestamp(event["timestamp"])}
            for event in events
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _distinct(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value != ""))


def _metadata_value(values: Iterable[str]) -> str | list[str] | None:
    unique = _distinct(values)
    return unique[0] if len(unique) == 1 else unique if unique else None


def cases_from_rows(
    columns: list[str], rows: list[dict[str, str]], *, min_events: int = 2,
    category: str | None = "C", country: str | None = None,
    year_start: int | None = None, year_end: int | None = None,
    document_index: dict[str, list[str]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Filter whole cases, preserving traces; min_events=-1 disables the event minimum."""
    if (isinstance(min_events, bool) or not isinstance(min_events, int)
            or (min_events != -1 and min_events < 1)):
        raise ValueError("min_events must be a positive integer or -1 for no minimum.")
    filter_years = year_start is not None or year_end is not None
    if filter_years:
        validate_year_range(year_start, year_end)
    if not _REQUIRED_LOG.issubset(columns):
        raise ValueError("The event log requires CaseID, Activity and Timestamp.")
    if category is not None and "Case_Category" not in columns:
        raise ValueError("Category filtering requires the recorded Case_Category column.")
    if country is not None:
        if not isinstance(country, str) or not country.strip():
            raise ValueError("country must be a non-empty recorded Country value or null for all cases.")
        if "Country" not in columns:
            raise ValueError("Country filtering requires the recorded Country column.")
    available_countries = sorted({row["Country"] for row in rows if row["Country"].strip()}) if "Country" in columns else []
    if country is not None and country not in available_countries:
        available = ", ".join(available_countries) or "(none)"
        raise ValueError(f"Unknown recorded Country value {country!r}. Available values: {available}.")
    grouped: dict[str, list[tuple[int, dict[str, str], str]]] = defaultdict(list)
    for source_row, row in enumerate(rows, 2):
        for key in _REQUIRED_LOG:
            if not row[key].strip():
                raise ValueError(f"Blank {key} at source record {source_row}.")
        if row["Activity"] != row["Activity"].strip():
            raise ValueError(f"Activity whitespace at source record {source_row}; labels are not renamed.")
        grouped[row["CaseID"]].append((source_row, row, normalised_timestamp(row["Timestamp"])))
    cases, register = [], []
    duplicate_total = 0
    eligible_before_country = 0
    excluded_country_cases = 0
    excluded_year_cases = 0
    eligible_after_country = 0
    eligible_after_year = 0
    excluded_document_cases = 0
    for case_id in sorted(grouped):
        records = grouped[case_id]
        categories = _distinct(record["Case_Category"] for _, record, _ in records) if "Case_Category" in columns else []
        countries = _distinct(record["Country"] for _, record, _ in records
                              if record["Country"].strip()) if "Country" in columns else []
        duplicate_excess = len(records) - len({tuple(record[column] for column in columns) for _, record, _ in records})
        duplicate_total += duplicate_excess
        reasons = []
        if min_events != -1 and len(records) < min_events:
            reasons.append(f"Fewer than {min_events} recorded events")
        if category is not None and categories != [category]:
            reasons.append("Recorded case category does not uniquely match " + category)
        otherwise_eligible = not reasons
        eligible_before_country += otherwise_eligible
        if country is not None and countries != [country]:
            excluded_country_cases += otherwise_eligible
            if not countries:
                reasons.append("No non-empty recorded Country value")
            elif len(countries) > 1:
                reasons.append("Conflicting recorded Country values: " + json.dumps(countries, ensure_ascii=False))
            else:
                reasons.append("Recorded Country does not match " + country)
        eligible_after_country += not reasons
        year = None
        if filter_years:
            try:
                year = case_year(case_id)
            except ValueError:
                if not reasons:
                    raise
            if year is not None and not year_start <= year <= year_end:
                excluded_year_cases += not reasons
                reasons.append(f"CaseID year {year} is outside {year_start}–{year_end}")
        eligible_after_year += not reasons
        document_files = document_index.get(case_document_prefix(case_id), []) if document_index is not None else []
        if document_index is not None and not document_files:
            excluded_document_cases += not reasons
            reasons.append("No non-empty .txt, .html or .pdf document in case_texts_dir")
        ordered = sorted(records, key=lambda item: (item[2], item[0]))
        timestamps: dict[str, set[str]] = defaultdict(set)
        for _, row, timestamp in ordered:
            timestamps[timestamp].add(row["Activity"])
        has_ties = any(len(labels) > 1 for labels in timestamps.values())
        register.append({
            "case_id": case_id,
            "CaseID": case_id,
            **({"case_year": year} if filter_years else {}),
            **({"document_file_count": len(document_files)} if document_index is not None else {}),
            "Country": countries[0] if len(countries) == 1 else json.dumps(countries, ensure_ascii=False) if countries else "",
            "event_count": len(records),
            "distinct_activity_count": len({row["Activity"] for _, row, _ in records}),
            "selection_status": "excluded" if reasons else "retained",
            "exclusion_reason": "; ".join(reasons),
            "has_timestamp_ties": has_ties,
            "identical_record_excess": duplicate_excess,
        })
        if reasons:
            continue
        metadata = {}
        for column in columns:
            if column == "CaseID" or column in _EVENT_FIELDS:
                continue
            value = _metadata_value(row[column] for _, row, _ in records)
            if value is not None:
                metadata[column] = value
        events = []
        for position, (source_row, row, _) in enumerate(ordered, 1):
            event_metadata = {column: row[column] for column in ("Activity_Party", "ECLI", "Date_Quality")
                              if column in columns and row[column] != ""}
            events.append({
                "ref": f"E{position:04d}",
                "activity": row["Activity"],
                "timestamp": row["Timestamp"],
                "source_row": source_row,
                "metadata": event_metadata,
            })
        cases.append({
            "case_id": case_id,
            **({"case_year": year} if filter_years else {}),
            **({"document_file_count": len(document_files), "document_files": list(document_files)}
               if document_index is not None else {}),
            "metadata": metadata,
            "events": events,
            "event_count": len(events),
            "trace_sha256": trace_hash(case_id, events),
            "has_timestamp_ties": has_ties,
            "identical_record_excess": duplicate_excess,
        })
    summary = {
        "source_events": len(rows),
        "source_cases": len(grouped),
        "selected_events": sum(case["event_count"] for case in cases),
        "selected_cases": len(cases),
        "excluded_cases": len(grouped) - len(cases),
        "excluded_one_event_cases": sum(row["event_count"] == 1 and row["selection_status"] == "excluded"
                                        for row in register),
        "minimum_recorded_events": min_events,
        "case_category": category,
        "country_filter": country,
        "available_countries": available_countries,
        "eligible_cases_before_country_filter": eligible_before_country,
        "eligible_cases_after_country_filter": eligible_after_country,
        "excluded_country_cases": excluded_country_cases,
        "case_identifier_handling": "CaseID values are preserved; there is no identifier-prefix filter.",
        "tie_break": "source",
        "deduplicate_exact": False,
        "exact_duplicate_records": duplicate_total,
        "selected_cases_with_timestamp_ties": sum(case["has_timestamp_ties"] for case in cases),
        "activity_vocabulary": sorted({row["Activity"] for row in rows}),
        "selected_activity_vocabulary": sorted({event["activity"] for case in cases for event in case["events"]}),
        "timestamp_interpretation": "Naive timestamps are compared as UTC for ordering; no observed time zone or within-date order is inferred.",
    }
    if filter_years:
        summary.update({
            "year_start": year_start, "year_end": year_end,
            "case_year_source": "Year in CaseID; two-digit years mean 2000–2099.",
            "eligible_cases_before_year_filter": eligible_after_country,
            "eligible_cases_after_year_filter": eligible_after_year,
            "excluded_year_cases": excluded_year_cases,
        })
    if document_index is not None:
        summary.update({
            "require_case_documents": True,
            "eligible_cases_before_document_filter": eligible_after_year,
            "eligible_cases_after_document_filter": len(cases),
            "excluded_document_cases": excluded_document_cases,
            "case_document_matching": "Exact downloader CaseID prefix and document sequence/total; non-empty .txt, .html or .pdf files, including downloaded originals.",
        })
    return cases, register, summary


def write_csv(path: str | Path, rows: list[dict[str, Any]], columns: list[str] | None = None) -> None:
    """Write a plain UTF-8 table without changing string fields."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if columns is None:
        columns = list(rows[0]) if rows else []
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def load_cases(
    path: str | Path, *, min_events: int = 2, category: str | None = "C",
    country: str | None = None, output_dir: str | Path | None = None,
    year_start: int | None = None, year_end: int | None = None,
    case_texts_dir: str | Path | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Load selected cases and optionally export the full cleaned source log."""
    path = Path(path)
    document_index = index_case_documents(case_texts_dir) if case_texts_dir is not None else None
    columns, rows = read_csv_text(path.read_text(encoding="utf-8-sig"), _REQUIRED_LOG)
    cases, register, summary = cases_from_rows(columns, rows, min_events=min_events,
                                             category=category, country=country,
                                             year_start=year_start, year_end=year_end,
                                             document_index=document_index)
    if not cases:
        detail = " including document availability in case_texts_dir" if document_index is not None else ""
        raise ValueError(f"No cases meet the eligibility criteria{detail}.")
    if case_texts_dir is not None:
        summary["case_texts_dir"] = str(Path(case_texts_dir).resolve())
    summary["source_sha256"] = sha256_file(path)
    summary["source_file"] = path.name
    if output_dir is not None:
        directory = Path(output_dir)
        directory.mkdir(parents=True, exist_ok=True)
        clean_path = directory / (path.stem + "_clean.csv")
        if clean_path.resolve() == path.resolve():
            raise ValueError("The cleaned log must not overwrite the original input.")
        retained_ids = {case["case_id"] for case in cases}
        with clean_path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns, delimiter=";")
            writer.writeheader()
            writer.writerows(row for row in rows if row["CaseID"] in retained_ids)
        write_csv(directory / "case_selection.csv", register)
        removed = [row["case_id"] for row in register if row["selection_status"] == "excluded"]
        lines = [
            "CURIA case selection and exclusions",
            f"Cases before selection: {summary['source_cases']}",
            f"Cases retained: {summary['selected_cases']}",
            f"Cases removed: {summary['excluded_cases']}",
            f"One-event cases removed: {summary['excluded_one_event_cases']}",
            f"Minimum recorded events: {min_events}" + (" (no minimum)" if min_events == -1 else ""),
            f"Recorded case category: {category or '(all)'}",
            f"Recorded Country filter: {country if country is not None else '(all)'}",
            f"Available recorded Country values: {', '.join(summary['available_countries']) or '(none)'}",
            f"Eligible cases before Country filtering: {summary['eligible_cases_before_country_filter']}",
            f"Eligible cases after Country filtering: {summary['eligible_cases_after_country_filter']}",
            f"Otherwise eligible cases removed by Country filtering: {summary['excluded_country_cases']}",
            *([
                f"CaseID year range (inclusive): {year_start}–{year_end}",
                f"Eligible cases before year filtering: {summary['eligible_cases_before_year_filter']}",
                f"Eligible cases after year filtering: {summary['eligible_cases_after_year_filter']}",
                f"Otherwise eligible cases removed by year filtering: {summary['excluded_year_cases']}",
            ] if year_start is not None else []),
            *([
                f"Case document directory: {summary['case_texts_dir']}",
                "Required documents: at least one non-empty .txt, .html or .pdf file per case",
                f"Eligible cases before document filtering: {summary['eligible_cases_before_document_filter']}",
                f"Eligible cases after document filtering: {summary['eligible_cases_after_document_filter']}",
                f"Otherwise eligible cases without documents: {summary['excluded_document_cases']}",
            ] if document_index is not None else []),
            "Timestamp ties retain source-row order. Exact duplicate events are retained.",
            "",
            "Removed CaseID values",
            *removed,
        ]
        (directory / "case_exclusions.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        summary["clean_event_log"] = str(clean_path)
        summary["clean_event_log_sha256"] = sha256_file(clean_path)
    return cases, register, summary


def select_cases(
    cases: list[dict[str, Any]], count: int | None = 20, seed: int = 20261117,
    case_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Select cases reproducibly; -1 or None selects all eligible cases."""
    if count is not None and (isinstance(count, bool) or not isinstance(count, int)
                              or (count != -1 and count < 1)):
        raise ValueError("The sample size must be a positive integer, -1 or null for all cases.")
    if count == -1:
        count = None
    ordered = sorted(cases, key=lambda case: case["case_id"])
    lookup = {case["case_id"]: case for case in ordered}
    if len(lookup) != len(ordered):
        raise ValueError("Case identifiers must be unique.")
    if case_ids is not None:
        if not case_ids or len(case_ids) != len(set(case_ids)):
            raise ValueError("Explicit case selection must be non-empty and contain no duplicates.")
        unknown = set(case_ids) - set(lookup)
        if unknown:
            raise ValueError("Unknown or excluded case identifiers: " + ", ".join(sorted(unknown)))
        return [lookup[identifier] for identifier in case_ids]
    if count is None:
        return ordered
    if count > len(ordered):
        raise ValueError(f"Requested {count} cases, but only {len(ordered)} are eligible.")
    chosen = random.Random(seed).sample(ordered, count)
    return sorted(chosen, key=lambda case: case["case_id"])

