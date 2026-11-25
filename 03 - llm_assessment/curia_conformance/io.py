"""Validate and select recorded judicial traces without changing activity labels."""

from __future__ import annotations

import csv
import hashlib
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


def load_and_select_log(
    path: str | Path,
    *,
    separator: str = ";",
    min_events: int = 2,
    case_prefix: str = "T-",
    tie_break: str = "source",
    deduplicate_exact: bool = False,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """Return selected events, a complete case register and input diagnostics.

    Event counts are row counts, including repeats unless exact-record
    deduplication is explicitly requested. Each distinct CaseID remains a trace;
    MainCaseID is not used to combine proceedings. Timestamp ties follow either
    source-row order or the legacy alphabetical convention. Neither convention
    establishes the actual order of procedural acts within a recorded date.
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
    records: list[dict[str, Any]] = []
    for case_id, events in frame.groupby("CaseID", sort=False):
        prefix_matches = not case_prefix or str(case_id).startswith(case_prefix)
        sufficient_events = len(events) >= min_events
        retained = prefix_matches and sufficient_events
        reasons = []
        if not prefix_matches:
            reasons.append("Case identifier does not match the selected prefix")
        if not sufficient_events:
            reasons.append(f"Fewer than {min_events} recorded events")
        records.append({
            "CaseID": str(case_id),
            "raw_event_count": int(original_counts.loc[case_id]),
            "event_count": len(events),
            "distinct_activity_count": int(events["Activity"].nunique()),
            "first_timestamp": events["Timestamp"].min().isoformat(),
            "last_timestamp": events["Timestamp"].max().isoformat(),
            "identical_record_excess": int(duplicate_counts.loc[case_id]),
            "has_timestamp_ties": case_id in ambiguous.index,
            "tied_timestamp_groups": int(ambiguous.get(case_id, 0)),
            "has_ecli": bool(events["ECLI"].str.strip().ne("").any()) if "ECLI" in events else False,
            "selection_status": "retained" if retained else "excluded",
            "exclusion_reason": "; ".join(reasons),
        })
    case_register = pd.DataFrame.from_records(records)
    retained_ids = set(case_register.loc[case_register["selection_status"] == "retained", "CaseID"])
    selected = frame.loc[frame["CaseID"].isin(retained_ids)].copy()
    if selected.empty:
        raise ValueError("No cases meet the selected eligibility criteria.")
    ordering = ["CaseID", "Timestamp"]
    if tie_break == "alphabetical":
        ordering.append("Activity")
    ordering.append("_source_row")
    selected = selected.sort_values(ordering, kind="mergesort").reset_index(drop=True)
    diagnostics = {
        "source_sha256": file_sha256(path),
        "source_events": int(original_counts.sum()),
        "source_cases": len(original_counts),
        "selected_events": len(selected),
        "selected_cases": len(retained_ids),
        "excluded_cases": len(original_counts) - len(retained_ids),
        "minimum_recorded_events": min_events,
        "case_prefix": case_prefix,
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
    """Save the selected original records and a readable case-exclusion report.

    The clean CSV retains original column names, values and timestamp strings.
    Its rows retain their original source order. The original input is
    never overwritten, and the subsequent checker reloads this clean file.
    """
    source_path, clean_path, report_path = map(Path, (source_path, clean_path, report_path))
    if source_path.resolve() == clean_path.resolve():
        raise ValueError("The clean event log must have a different path from the source.")
    if report_path.resolve() in {source_path.resolve(), clean_path.resolve()}:
        raise ValueError("The exclusion report must not overwrite an event log.")
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
    one_event_removed = int(excluded["event_count"].eq(1).sum())
    lines = [
        "CURIA case selection and exclusions",
        "===================================",
        f"Source event log: {source_path.name}",
        f"Clean event log: {clean_path.name}",
        f"Minimum recorded events per case: {diagnostics['minimum_recorded_events']}",
        f"Case identifier prefix: {diagnostics['case_prefix'] or '(all prefixes)'}",
        f"Cases before selection: {diagnostics['source_cases']}",
        f"Cases removed: {len(removed_ids)}",
        f"Cases retained: {diagnostics['selected_cases']}",
        f"One-event cases removed: {one_event_removed}",
        f"Events before selection: {diagnostics['source_events']}",
        f"Events retained: {diagnostics['selected_events']}",
        f"Exact duplicate removal enabled: {diagnostics['deduplicate_exact']}",
        "",
        "The threshold counts recorded event occurrences, not distinct activity labels.",
        "Exclusion is an operational selection criterion and does not establish legal incompleteness.",
        "Detailed reasons are recorded in case_selection.csv.",
        "",
        "Removed CaseID values (one per line)",
        "----------------------------------",
    ]
    lines.extend(removed_ids if removed_ids else ["No cases were removed."])
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "clean_event_log": str(clean_path),
        "clean_event_log_sha256": file_sha256(clean_path),
        "exclusion_report": str(report_path),
        "removed_case_count": len(removed_ids),
        "removed_one_event_case_count": one_event_removed,
    }
