"""Validate and select recorded judicial traces without changing activity labels."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Any

import pandas as pd

REQUIRED_COLUMNS = ("CaseID", "Activity", "Timestamp")


def file_sha256(path: str | Path) -> str:
    """Identify the exact source bytes used by an experiment."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_endpoint_events(path: str | Path) -> dict[str, list[str]]:
    """Read exact initial and final activity labels from a JSON configuration."""
    try:
        configuration = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid endpoint-event configuration JSON: {exc}") from exc
    if not isinstance(configuration, dict) or set(configuration) != {"initial", "final"}:
        raise ValueError("The endpoint-event configuration must contain exactly 'initial' and 'final'.")
    validated: dict[str, list[str]] = {}
    for key in ("initial", "final"):
        activities = configuration[key]
        if not isinstance(activities, list) or not activities:
            raise ValueError(f"Endpoint-event '{key}' must be a non-empty list of activity labels.")
        validated[key] = []
        for activity in activities:
            if not isinstance(activity, str) or not activity.strip():
                raise ValueError(f"Endpoint-event '{key}' must contain only non-empty activity labels.")
            validated[key].append(activity)
    return validated


def load_and_select_log(
    path: str | Path,
    *,
    separator: str = ";",
    min_events: int = 2,
    tie_break: str = "source",
    deduplicate_exact: bool = False,
    endpoint_events: dict[str, list[str]] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """Return selected events, a complete case register and input diagnostics.

    Event counts are row counts, including repeats unless exact-record
    deduplication is explicitly requested. CaseID values are preserved unchanged
    and never used as selection criteria. Each distinct CaseID remains a trace;
    MainCaseID is not used to combine proceedings. Timestamp ties follow either
    source-row order or the legacy alphabetical convention. Neither convention
    establishes the actual order of procedural acts within a recorded date.
    When configured, initial and final activities are checked in that same
    ordering after the minimum event count has been applied.
    """
    path = Path(path)
    if min_events < 1:
        raise ValueError("The minimum event count must be at least one.")
    if len(separator) != 1:
        raise ValueError("The CSV separator must be one character.")
    if tie_break not in {"source", "alphabetical"}:
        raise ValueError("The tie convention must be 'source' or 'alphabetical'.")
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.reader(stream, delimiter=separator)
        columns = next(reader, [])
    if len(columns) != len(set(columns)):
        raise ValueError("The event log contains duplicate column names.")
    if "_source_row" in columns:
        raise ValueError("The event log uses the reserved column '_source_row'.")
    missing = [column for column in REQUIRED_COLUMNS if column not in columns]
    if missing:
        raise ValueError("Missing event-log columns: " + ", ".join(missing))

    frame = pd.read_csv(
        path, sep=separator, encoding="utf-8-sig", dtype=str,
        keep_default_na=False, na_filter=False,
    )
    if frame.empty:
        raise ValueError("The event log contains no events.")
    frame["_source_row"] = range(2, len(frame) + 2)
    for column in REQUIRED_COLUMNS:
        invalid = frame[column].str.strip().eq("")
        if invalid.any():
            records = frame.loc[invalid, "_source_row"].head(10).tolist()
            raise ValueError(f"Blank {column} values at source records {records}.")

    original_counts = frame.groupby("CaseID", sort=False).size()
    duplicate_mask = frame.duplicated(subset=columns, keep="first")
    duplicate_counts = duplicate_mask.groupby(frame["CaseID"], sort=False).sum()
    duplicate_excess = int(duplicate_mask.sum())
    if deduplicate_exact:
        frame = frame.loc[~duplicate_mask].copy()

    # UTC gives a common comparison type; naive dates carry no observed time zone.
    try:
        frame["Timestamp"] = pd.to_datetime(
            frame["Timestamp"], errors="raise", format="mixed", utc=True,
        )
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Invalid event timestamps: {exc}") from exc
    if frame["Timestamp"].isna().any():
        raise ValueError("The event log contains missing timestamps after parsing.")

    tie_groups = frame.groupby(["CaseID", "Timestamp"], sort=False)["Activity"].nunique()
    ambiguous = tie_groups[tie_groups > 1].groupby(level=0).size()
    ordering = ["CaseID", "Timestamp"]
    if tie_break == "alphabetical":
        ordering.append("Activity")
    ordering.append("_source_row")
    ordered = frame.sort_values(ordering, kind="mergesort")
    endpoints = ordered.groupby("CaseID", sort=False)["Activity"].agg(["first", "last"])
    allowed_initial = set(endpoint_events["initial"]) if endpoint_events is not None else set()
    allowed_final = set(endpoint_events["final"]) if endpoint_events is not None else set()
    records: list[dict[str, Any]] = []
    for case_id, events in frame.groupby("CaseID", sort=False):
        first_activity = str(endpoints.loc[case_id, "first"])
        last_activity = str(endpoints.loc[case_id, "last"])
        excluded_by_min_events = len(events) < min_events
        endpoint_reasons = []
        if endpoint_events is not None:
            if first_activity not in allowed_initial:
                endpoint_reasons.append("Initial activity is not in the configured initial list")
            if last_activity not in allowed_final:
                endpoint_reasons.append("Final activity is not in the configured final list")
        excluded_by_endpoints = not excluded_by_min_events and bool(endpoint_reasons)
        retained = not excluded_by_min_events and not excluded_by_endpoints
        exclusion_reason = (
            f"Fewer than {min_events} recorded events" if excluded_by_min_events
            else "; ".join(endpoint_reasons)
        )
        records.append({
            "CaseID": str(case_id),
            "raw_event_count": int(original_counts.loc[case_id]),
            "event_count": len(events),
            "distinct_activity_count": int(events["Activity"].nunique()),
            "first_timestamp": events["Timestamp"].min().isoformat(),
            "last_timestamp": events["Timestamp"].max().isoformat(),
            "first_activity": first_activity,
            "last_activity": last_activity,
            "identical_record_excess": int(duplicate_counts.loc[case_id]),
            "has_timestamp_ties": case_id in ambiguous.index,
            "tied_timestamp_groups": int(ambiguous.get(case_id, 0)),
            "has_ecli": bool(events["ECLI"].str.strip().ne("").any()) if "ECLI" in events else False,
            "selection_status": "retained" if retained else "excluded",
            "exclusion_reason": exclusion_reason,
            "excluded_by_min_events": excluded_by_min_events,
            "excluded_by_endpoints": excluded_by_endpoints,
        })
    case_register = pd.DataFrame.from_records(records)
    retained_ids = set(case_register.loc[case_register["selection_status"] == "retained", "CaseID"])
    selected = frame.loc[frame["CaseID"].isin(retained_ids)].copy()
    if selected.empty:
        endpoint_description = "; endpoint activities filter enabled" if endpoint_events is not None else ""
        raise ValueError(
            "No cases meet the selected eligibility criteria "
            f"(minimum recorded events: {min_events}{endpoint_description})."
        )
    selected = selected.sort_values(ordering, kind="mergesort").reset_index(drop=True)
    diagnostics = {
        "source_sha256": file_sha256(path),
        "source_events": int(original_counts.sum()),
        "source_cases": len(original_counts),
        "selected_events": len(selected),
        "selected_cases": len(retained_ids),
        "excluded_cases": len(original_counts) - len(retained_ids),
        "minimum_recorded_events": min_events,
        "endpoint_events": endpoint_events,
        "excluded_cases_min_events": int(case_register["excluded_by_min_events"].sum()),
        "excluded_cases_endpoints": int(case_register["excluded_by_endpoints"].sum()),
        "case_identifier_handling": "CaseID values are preserved unchanged; no identifier-based filtering.",
        "tie_break": tie_break,
        "exact_duplicate_records": duplicate_excess,
        "deduplicate_exact": deduplicate_exact,
        "selected_cases_with_timestamp_ties": int(case_register.loc[case_register["selection_status"] == "retained", "has_timestamp_ties"].sum()),
        "timestamp_interpretation": "Recorded dates are compared in UTC; no within-date procedural order is inferred.",
        "activity_vocabulary": sorted(frame["Activity"].unique().tolist()),
    }
    return selected, case_register, diagnostics


def save_clean_log(
    source_path: str | Path,
    clean_path: str | Path,
    selected_events: pd.DataFrame,
    case_register: pd.DataFrame,
    diagnostics: dict[str, Any],
    report_path: str | Path,
    separator: str = ";",
) -> dict[str, Any]:
    """Save retained and excluded original records and a case-exclusion report.

    Both CSV files retain original columns, values, timestamp strings and source
    order. The excluded CSV includes all original records of excluded cases,
    including repeats when exact-record deduplication is enabled. The source is
    never overwritten, and the subsequent checker reloads the clean file.
    """
    source_path, clean_path, report_path = map(Path, (source_path, clean_path, report_path))
    excluded_path = report_path.parent / f"{source_path.stem}_excluded.csv"
    if source_path.resolve() == clean_path.resolve():
        raise ValueError("The clean event log must have a different path from the source.")
    if report_path.resolve() in {source_path.resolve(), clean_path.resolve()}:
        raise ValueError("The exclusion report must not overwrite an event log.")
    if excluded_path.resolve() in {source_path.resolve(), clean_path.resolve(), report_path.resolve()}:
        raise ValueError("The excluded event log must have a different path from the source, clean log and report.")
    with source_path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, delimiter=separator)
        columns = reader.fieldnames
        raw_records = list(reader)
    clean_path.parent.mkdir(parents=True, exist_ok=True)
    with clean_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, delimiter=separator)
        writer.writeheader()
        for position in sorted(selected_events["_source_row"]):
            writer.writerow(raw_records[int(position) - 2])

    excluded = case_register.loc[case_register["selection_status"] == "excluded"]
    removed_ids = sorted(excluded["CaseID"].tolist())
    min_events_removed_ids = sorted(excluded.loc[excluded["excluded_by_min_events"], "CaseID"].tolist())
    endpoint_removed_ids = sorted(excluded.loc[excluded["excluded_by_endpoints"], "CaseID"].tolist())
    removed_id_set = set(removed_ids)
    excluded_event_count = 0
    excluded_path.parent.mkdir(parents=True, exist_ok=True)
    with excluded_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, delimiter=separator)
        writer.writeheader()
        for record in raw_records:
            if record["CaseID"] in removed_id_set:
                writer.writerow(record)
                excluded_event_count += 1
    one_event_removed = int(excluded["event_count"].eq(1).sum())
    lines = [
        "CURIA case selection and exclusions",
        "===================================",
        f"Source event log: {source_path.name}",
        f"Clean event log: {clean_path.name}",
        f"Excluded event log: {excluded_path.name}",
        f"Minimum recorded events per case: {diagnostics['minimum_recorded_events']}",
        f"Endpoint activities filter enabled: {diagnostics['endpoint_events'] is not None}",
        "Case identifiers: all values, preserved unchanged",
        f"Cases before selection: {diagnostics['source_cases']}",
        f"Cases removed: {len(removed_ids)}",
        f"Cases removed by minimum event count: {len(min_events_removed_ids)}",
        f"Cases removed by endpoint activities: {len(endpoint_removed_ids)}",
        f"Cases retained: {diagnostics['selected_cases']}",
        f"One-event cases removed: {one_event_removed}",
        f"Events before selection: {diagnostics['source_events']}",
        f"Events retained: {diagnostics['selected_events']}",
        f"Original events in excluded cases: {excluded_event_count}",
        f"Exact duplicate removal enabled: {diagnostics['deduplicate_exact']}",
        "",
        "The threshold counts recorded event occurrences, not distinct activity labels.",
        "Exclusion is an operational selection criterion and does not establish legal incompleteness.",
        "Detailed reasons are recorded in case_selection.csv.",
        "",
        "CaseID values excluded by minimum event count",
        "--------------------------------------------",
    ]
    lines.extend(min_events_removed_ids if min_events_removed_ids else ["No cases were removed by minimum event count."])
    lines.extend([
        "",
        "CaseID values excluded by endpoint activities",
        "--------------------------------------------",
    ])
    lines.extend(endpoint_removed_ids if endpoint_removed_ids else ["No cases were removed by endpoint activities."])
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "clean_event_log": str(clean_path),
        "clean_event_log_sha256": file_sha256(clean_path),
        "excluded_event_log": str(excluded_path),
        "excluded_event_log_sha256": file_sha256(excluded_path),
        "excluded_event_count": excluded_event_count,
        "exclusion_report": str(report_path),
        "removed_case_count": len(removed_ids),
        "removed_one_event_case_count": one_event_removed,
        "removed_min_events_case_count": len(min_events_removed_ids),
        "removed_endpoint_case_count": len(endpoint_removed_ids),
    }
