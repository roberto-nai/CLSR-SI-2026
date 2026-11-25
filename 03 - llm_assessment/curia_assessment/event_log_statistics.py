"""Describe selected recorded traces without weighting case–rule duplicates."""

from __future__ import annotations

import csv
import io
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from statistics import fmean, median
from typing import Any, Iterable, Mapping

from .inputs import case_year, normalised_timestamp
from .text_evidence import _document_groups


def summarise_event_log(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Summarise each distinct CaseID once, preserving recorded activity order.

    Records may be selected cases or their repeated case–rule pairs. Copies of
    one CaseID must contain exactly the same complete event objects. Durations
    are elapsed calendar seconds between a case's earliest and latest recorded
    event, not business time or independently established proceeding duration.
    Naive timestamps follow the input loader's UTC convention.
    """
    traces: dict[str, list[Mapping[str, Any]]] = {}
    document_files: dict[str, frozenset[str]] = {}
    declared_file_counts: dict[str, int] = {}
    for record in records:
        identifier = record.get("case_id")
        if not isinstance(identifier, str) or not identifier.strip():
            raise ValueError("Event-log statistics require a non-empty case_id.")
        events = record.get("events")
        if not isinstance(events, (list, tuple)) or not events:
            raise ValueError(f"{identifier}: event-log statistics require recorded events.")
        events = list(events)
        declared_count = record.get("document_file_count")
        if declared_count is not None:
            if isinstance(declared_count, bool) or not isinstance(declared_count, int) or declared_count < 0:
                raise ValueError(f"{identifier}: document_file_count must be a non-negative integer.")
            if identifier in declared_file_counts and declared_file_counts[identifier] != declared_count:
                raise ValueError(f"{identifier}: conflicting document_file_count values in repeated case records.")
            declared_file_counts[identifier] = declared_count
        files = record.get("document_files")
        if files is not None:
            if (not isinstance(files, (list, tuple))
                    or any(not isinstance(name, str) or not name.strip() for name in files)):
                raise ValueError(f"{identifier}: document_files must contain non-empty relative file names.")
            unique_files = frozenset(files)
            if any(Path(name).suffix.lower() not in {".txt", ".html", ".pdf"} for name in unique_files):
                raise ValueError(f"{identifier}: unsupported format in document_files.")
            if identifier in document_files and document_files[identifier] != unique_files:
                raise ValueError(f"{identifier}: conflicting document_files in repeated case records.")
            document_files[identifier] = unique_files
        if identifier in document_files and identifier in declared_file_counts:
            if len(document_files[identifier]) != declared_file_counts[identifier]:
                raise ValueError(f"{identifier}: document_file_count differs from the distinct document_files count.")
        if identifier in traces:
            if traces[identifier] != events:
                raise ValueError(f"{identifier}: conflicting event traces in repeated case records.")
            continue
        traces[identifier] = events

    variants: set[tuple[str, ...]] = set()
    activities: set[str] = set()
    years: Counter[int] = Counter()
    durations: list[float] = []
    timestamps: list[datetime] = []
    event_count = tie_cases = 0
    for identifier, events in traces.items():
        years[case_year(identifier)] += 1
        labels = []
        instants = []
        for event in events:
            if not isinstance(event, Mapping):
                raise ValueError(f"{identifier}: recorded events must be objects.")
            activity = event.get("activity")
            if not isinstance(activity, str) or not activity.strip():
                raise ValueError(f"{identifier}: missing or invalid recorded activity.")
            timestamp = event.get("timestamp")
            if not isinstance(timestamp, str) or not timestamp.strip():
                raise ValueError(f"{identifier}: missing or invalid recorded timestamp.")
            instant = datetime.fromisoformat(normalised_timestamp(timestamp))
            labels.append(activity)
            instants.append(instant)
        activities.update(labels)
        variants.add(tuple(labels))
        event_count += len(events)
        timestamps.extend(instants)
        durations.append((max(instants) - min(instants)).total_seconds())
        tie_cases += len(set(instants)) < len(instants)

    mean_seconds = fmean(durations) if durations else None
    median_seconds = median(durations) if durations else None
    upper_median_seconds = sorted(durations)[len(durations) // 2] if durations else None
    document_coverage_complete = len(document_files) == len(traces)
    document_counts = [len(_document_groups(sorted(files))) for files in document_files.values()]
    file_counts = [len(files) for files in document_files.values()]
    return {
        "n_cases": len(traces),
        "n_events": event_count,
        "n_activities": len(activities),
        "n_variants": len(variants),
        "min_timestamp": min(timestamps).isoformat() if timestamps else None,
        "max_timestamp": max(timestamps).isoformat() if timestamps else None,
        "mean_case_duration_seconds": mean_seconds,
        "median_case_duration_seconds": median_seconds,
        "mean_case_duration_days": mean_seconds / 86400 if mean_seconds is not None else None,
        "median_case_duration_days": median_seconds / 86400 if median_seconds is not None else None,
        "pm4py_upper_median_case_duration_seconds": upper_median_seconds,
        "pm4py_upper_median_case_duration_days": (
            upper_median_seconds / 86400 if upper_median_seconds is not None else None
        ),
        "n_cases_with_timestamp_ties": tie_cases,
        "cases_by_year": {str(year): years[year] for year in sorted(years)},
        "n_cases_with_document_metadata": len(document_files),
        "n_document_files": sum(file_counts) if document_coverage_complete else None,
        "mean_document_files_per_case": fmean(file_counts) if document_coverage_complete and traces else None,
        "n_documents": sum(document_counts) if document_coverage_complete else None,
        "mean_documents_per_case": fmean(document_counts) if document_coverage_complete and traces else None,
        "median_documents_per_case": median(document_counts) if document_coverage_complete and traces else None,
        "min_documents_per_case": min(document_counts) if document_coverage_complete and traces else None,
        "max_documents_per_case": max(document_counts) if document_coverage_complete and traces else None,
    }


def format_event_log_statistics(stats: Mapping[str, Any]) -> str:
    """Return a British-English description with explicit counting conventions."""
    def duration(name: str) -> str:
        seconds, days = stats[name + "_seconds"], stats[name + "_days"]
        return "unavailable" if seconds is None else f"{days:,.3f} days ({seconds:,.3f} seconds)"

    def document_metric(name: str, *, decimal: bool = False) -> str:
        value = stats.get(name)
        return "unavailable" if value is None else f"{value:,.3f}" if decimal else str(value)

    years = "; ".join(f"{year}: {count}" for year, count in stats["cases_by_year"].items())
    lines = [
        "EVENT-LOG CHARACTERISTICS",
        f"Distinct selected cases: {stats['n_cases']}",
        f"Total recorded events: {stats['n_events']}",
        f"Distinct activity labels: {stats['n_activities']}",
        f"Distinct activity-sequence variants: {stats['n_variants']}",
        f"Earliest recorded event (UTC): {stats['min_timestamp'] or 'unavailable'}",
        f"Latest recorded event (UTC): {stats['max_timestamp'] or 'unavailable'}",
        f"Mean observed case duration: {duration('mean_case_duration')}",
        f"Median observed case duration: {duration('median_case_duration')}",
        f"PM4Py upper-middle case duration: {duration('pm4py_upper_median_case_duration')}",
        f"Cases with tied event timestamps: {stats['n_cases_with_timestamp_ties']}",
        f"Selected cases by CaseID year: {years or '(none)'}",
        f"Cases with recorded document metadata: {document_metric('n_cases_with_document_metadata')}/{stats['n_cases']}",
        f"Associated distinct documents: {document_metric('n_documents')}",
        f"Mean associated documents per case: {document_metric('mean_documents_per_case', decimal=True)}",
        f"Median associated documents per case: {document_metric('median_documents_per_case', decimal=True)}",
        f"Minimum/maximum associated documents per case: {document_metric('min_documents_per_case')}/{document_metric('max_documents_per_case')}",
        f"Associated document files (all recorded formats): {document_metric('n_document_files')}",
        f"Mean associated document files per case: {document_metric('mean_document_files_per_case', decimal=True)}",
        "Each selected CaseID is counted once, independently of rules, models and repeated requests. "
        "Variants are exact sequences of recorded activity labels in the frozen event order; "
        "timestamps, event metadata and case identifiers do not distinguish variants.",
        "Durations use the latest minus earliest recorded timestamp per case, in elapsed calendar time "
        "(one day = 86,400 seconds). They describe the observed traces and do not establish complete "
        "proceeding duration. Timestamp ties retain the frozen source order when distinguishing variants. "
        "Naive timestamps are treated as UTC, following the input loader.",
        "The median is the conventional statistical median: the average of the two central durations "
        "for an even number of cases. The separate PM4Py upper-middle value reproduces the EventLog "
        "get_median_case_duration helper's upper-central convention.",
        "Associated documents are counted once per case and relative document stem, grouping TXT, HTML "
        "and PDF copies and removing the _unconverted suffix, using the same grouping as document evidence. "
        "Distinct stems remain distinct documents even when their text is identical. Missing document "
        "metadata remains unavailable rather than zero; cohort totals and means require metadata for every "
        "selected case. Association does not imply that documents were supplied to the models in this phase.",
    ]
    return "\n".join(lines) + "\n"


def write_event_log_statistics(
    output: str | Path, records: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Write deterministic JSON, one-row CSV and text outputs atomically per file."""
    stats = summarise_event_log(records)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    csv_stream = io.StringIO(newline="")
    writer = csv.DictWriter(csv_stream, fieldnames=list(stats))
    writer.writeheader()
    writer.writerow({
        **stats,
        "cases_by_year": json.dumps(stats["cases_by_year"], ensure_ascii=False, sort_keys=True),
    })
    contents = {
        "event_log_statistics.json": json.dumps(stats, ensure_ascii=False, indent=2) + "\n",
        "event_log_statistics.csv": csv_stream.getvalue(),
        "event_log_statistics.txt": format_event_log_statistics(stats),
    }
    for name, content in contents.items():
        destination = output / name
        temporary = destination.with_name(destination.name + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(destination)
    return stats
