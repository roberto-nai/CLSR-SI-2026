"""Report measured API resources per model, phase and repetition.

These are case-request resources across all retained attempts, including failed
attempts and imported responses. API seconds sum individual request durations;
they are neither wall-clock phase duration nor CPU time. Parallel request times
can overlap. Token/cache/reasoning semantics come from metrics rather than an
additional calculation here. Unknown measurements remain empty CSV cells.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path
from typing import Any

_GROUP_FIELDS = ("provider", "model", "condition", "run")
_FIELDS = (
    "phase", "provider_id", "provider", "model", "run", "planned_case_requests",
    "n_recorded_attempts", "n_api_calls", "n_reused_responses", "n_new_api_calls",
    "known_call_attempt_flags", "unknown_call_attempt_flags",
    "input_tokens", "output_tokens", "total_tokens", "api_seconds_total", "api_seconds_mean",
    "measured_elapsed_attempts", "measured_input_token_attempts", "measured_output_token_attempts",
    "measured_total_token_attempts",
)


def _group(row: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(row.get(field) for field in _GROUP_FIELDS)


def _reused(row: dict[str, Any]) -> bool:
    return bool(row.get("imported_from")) or row.get("imported_attempt") is True


def write_model_resources(
    output: str | Path, *, jobs: list[dict[str, Any]], ledger: list[dict[str, Any]],
    usage_summary: list[dict[str, Any]],
) -> Path:
    """Write UTF-8 BOM CSV without expanding case calls into rule judgements.

    Pass case jobs and actual attempt rows, not the virtual rule-level rows.
    ``usage_summary`` must be produced by ``metrics.summarise_resource_usage``
    for those jobs/attempts. Its known sums and measurement coverage are retained.
    Known API-call counts are lower bounds when call-attempt flags are missing;
    wholly unknown flags leave counts blank. Zero archived attempts is known zero.
    Imported responses retain their original API tokens/time in these totals but
    are excluded from the count of newly transmitted API calls.
    """
    planned: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    attempts: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    seen_jobs = set()
    for job in jobs:
        identifier = job.get("job_id")
        if identifier is not None:
            if identifier in seen_jobs:
                raise ValueError("Model resource reporting requires unique case jobs.")
            seen_jobs.add(identifier)
        planned[_group(job)].append(job)
    seen_attempts = set()
    for row in ledger:
        identifier, attempt = row.get("job_id"), row.get("attempt")
        if identifier is not None and attempt is not None:
            key = (identifier, str(attempt))
            if key in seen_attempts:
                raise ValueError("An actual API attempt must not be repeated in resource reporting.")
            seen_attempts.add(key)
        attempts[_group(row)].append(row)
    summaries = {}
    for row in usage_summary:
        key = _group(row)
        if key in summaries:
            raise ValueError("Duplicate model/phase/run group in resource usage summary.")
        summaries[key] = row
    groups = planned.keys() | attempts.keys() | summaries.keys()
    rows = []
    for key in sorted(groups, key=lambda values: tuple("" if value is None else str(value) for value in values)):
        if key not in summaries:
            raise ValueError("A model/phase/run group has no measured resource summary.")
        summary = summaries[key]
        recorded = attempts[key]
        if summary.get("n_attempts") != len(recorded):
            raise ValueError("Resource summary attempt count differs from the actual attempt ledger.")
        known_flags = [row for row in recorded if isinstance(row.get("call_attempted"), bool)]
        calls = [row for row in recorded if row.get("call_attempted") is True]
        imported_calls = sum(_reused(row) for row in calls)
        calls_known = bool(known_flags) or not recorded
        provider_ids = sorted({row["provider_id"] for row in [*planned[key], *recorded]
                               if isinstance(row.get("provider_id"), str) and row["provider_id"]})
        rows.append({
            "phase": key[2], "provider_id": ";".join(provider_ids), "provider": key[0], "model": key[1], "run": key[3],
            "planned_case_requests": len(planned[key]), "n_recorded_attempts": summary["n_attempts"],
            "n_api_calls": len(calls) if calls_known else None,
            "n_reused_responses": sum(_reused(row) for row in recorded),
            "n_new_api_calls": len(calls) - imported_calls if calls_known else None,
            "known_call_attempt_flags": len(known_flags), "unknown_call_attempt_flags": len(recorded) - len(known_flags),
            "input_tokens": summary.get("input_tokens_total"), "output_tokens": summary.get("output_tokens_total"),
            "total_tokens": summary.get("total_tokens_total"), "api_seconds_total": summary.get("elapsed_seconds_total"),
            "api_seconds_mean": summary.get("elapsed_seconds_mean"),
            "measured_elapsed_attempts": summary.get("n_attempts_with_elapsed_seconds"),
            "measured_input_token_attempts": summary.get("n_attempts_with_input_tokens"),
            "measured_output_token_attempts": summary.get("n_attempts_with_output_tokens"),
            "measured_total_token_attempts": summary.get("n_attempts_with_total_tokens"),
        })
    path = Path(output) / "model_resources.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)
    return path
