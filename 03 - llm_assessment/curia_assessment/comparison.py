"""Compare LLM decisions with formal DECLARE outcomes.

These comparisons measure agreement with the supplied formal baseline, not
legal correctness. Vacuous satisfaction remains formal satisfaction, while
the comparison exposes Agreement, Disagreement and Undetermined. Legal
uncertainty yields an Undetermined comparison. Unexpected outcome labels are
invalid outputs rather than substantive comparison categories.

Each input prediction represents one planned model/condition/run assessment
of one case and one textual requirement variant. Several variants may share
one original DECLARE rule. Counts expose both units rather than treating the
variants as independent original rules.
"""

from __future__ import annotations

from collections import Counter, defaultdict
import json
from typing import Any, Iterable, Mapping


OUTCOMES = frozenset({"compliance", "violation", "undetermined"})
BINARY_OUTCOMES = frozenset({"compliance", "violation"})
FORMAL_OUTCOMES = frozenset({"satisfied", "violated", "vacuously_satisfied"})
ACTIVATION_STATUSES = frozenset({"active", "inactive", "not_applicable"})
COMPARISON_CATEGORIES = ("Agreement", "Disagreement", "Undetermined")
_NOT_RUN_STATUSES = frozenset({"pending", "planned", "not_run", "dry_run", "skipped"})
_MISSING_STATUSES = frozenset({"missing", "missing_output", "missing_result"})
_INVALID_STATUSES = frozenset({
    "invalid_json", "invalid_output", "invalid_outcome", "invalid_schema",
    "invalid_evidence", "validation_error",
})
_MODEL_FIELDS = ("provider", "model", "condition", "run")


def _ratio(numerator: int, denominator: int) -> float | None:
    """Leave ratios with empty denominators unavailable."""
    return numerator / denominator if denominator else None


def _identifier(row: Mapping[str, Any], names: tuple[str, ...], kind: str) -> str:
    """Read compatible identifier aliases without silently resolving conflicts."""
    values = [row[name] for name in names if name in row and row[name] is not None]
    if not values:
        raise ValueError(f"Missing {kind}; expected one of {names}.")
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError(f"{kind} must be a non-empty string.")
    if len(set(values)) != 1:
        raise ValueError(f"Conflicting aliases for {kind}: {names}.")
    return values[0]


def _boolean(value: Any, field: str) -> bool:
    """Accept genuine Boolean values and their ordinary CSV encodings."""
    if value is True or value == 1:
        return True
    if value is False or value == 0:
        return False
    if isinstance(value, str) and value.strip().lower() in {"true", "1"}:
        return True
    if isinstance(value, str) and value.strip().lower() in {"false", "0"}:
        return False
    raise ValueError(f"{field} must be a Boolean value or its CSV encoding.")


def _formal_index(
    formal_results: Iterable[Mapping[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    """Validate one formal result for each original case--rule pair."""
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for original in formal_results:
        row = dict(original)
        case_id = _identifier(row, ("case_id", "CaseID"), "formal case identifier")
        rule_id = _identifier(row, ("base_rule_id", "rule_id"), "formal rule identifier")
        key = (case_id, rule_id)
        if key in index:
            raise ValueError(f"Duplicate formal result for {case_id!r}, {rule_id!r}.")
        state = row.get("formal_satisfied")
        if isinstance(state, str) and state in {"0", "1"}:
            state = int(state)
        if not isinstance(state, (int, bool)) or state not in {0, 1}:
            raise ValueError("formal_satisfied must be 0 or 1, with 1 meaning satisfied.")
        outcome = row.get("formal_outcome")
        if outcome not in FORMAL_OUTCOMES:
            raise ValueError(f"Unknown or missing formal_outcome: {outcome!r}.")
        activation = row.get("activation_status")
        if activation not in ACTIVATION_STATUSES:
            raise ValueError(f"Unknown or missing activation_status: {activation!r}.")
        vacuous = _boolean(row.get("vacuously_satisfied"), "vacuously_satisfied")
        if int(state) != int(outcome != "violated"):
            raise ValueError("formal_satisfied disagrees with formal_outcome.")
        if vacuous != (outcome == "vacuously_satisfied"):
            raise ValueError("vacuously_satisfied disagrees with formal_outcome.")
        if vacuous and activation != "inactive":
            raise ValueError("Vacuous satisfaction requires inactive activation status.")
        if activation == "inactive" and not vacuous:
            raise ValueError("An inactive result must be vacuously satisfied.")
        row.update(
            case_id=case_id,
            base_rule_id=rule_id,
            formal_satisfied=int(state),
            vacuously_satisfied=vacuous,
        )
        index[key] = row
    return index


def compare_rows(
    predictions: Iterable[Mapping[str, Any]],
    formal_results: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Join every planned prediction to its original formal case--rule result.

    All supplied prediction metadata is preserved. Both canonical identifiers
    and the previous checker's CaseID/rule_id aliases are accepted.

    A valid LLM label requires status='ok' and a recognised current outcome.
    Only compliance/violation decisions enter agreement rates. The public
    comparison_category has exactly three possible labels. An unexpected
    outcome label is an invalid output, never an agreement or legal uncertainty.
    Missing formal results, non-binary outcomes and generation/format failures
    remain explicit rows; none is silently treated as disagreement.

    The caller must supply one row for every planned job, including pending
    jobs, and must verify the baseline's trace/catalogue provenance before
    calling this function.
    """
    formal_index = _formal_index(formal_results)
    joined: list[dict[str, Any]] = []
    seen_jobs: set[str] = set()
    seen_assessments: set[tuple[Any, ...]] = set()
    for original in predictions:
        row = dict(original)
        case_id = _identifier(row, ("case_id", "CaseID"), "prediction case identifier")
        rule_id = _identifier(row, ("base_rule_id", "rule_id"), "prediction rule identifier")
        variant_id = row.get("variant_id", rule_id)
        if not isinstance(variant_id, str) or not variant_id.strip():
            raise ValueError("variant_id must be a non-empty string.")
        model_key = tuple(str(row.get(field, "")) for field in _MODEL_FIELDS)
        assessment_key = (*model_key, case_id, variant_id)
        if assessment_key in seen_assessments:
            raise ValueError(f"Duplicate planned assessment for {assessment_key!r}.")
        seen_assessments.add(assessment_key)
        job_id = row.get("job_id")
        if job_id is not None:
            if not isinstance(job_id, str) or not job_id.strip():
                raise ValueError("job_id must be a non-empty string when supplied.")
            if job_id in seen_jobs:
                raise ValueError(f"Duplicate job_id: {job_id!r}.")
            seen_jobs.add(job_id)

        status = row.get("status")
        if status is None or status == "":
            status = "missing_output"
        if not isinstance(status, str):
            raise ValueError("Prediction status must be a string.")
        outcome = row.get("outcome")
        valid = status == "ok" and isinstance(outcome, str) and outcome in OUTCOMES
        output_status = "invalid_outcome" if status == "ok" and not valid else status
        binary = valid and outcome in BINARY_OUTCOMES
        formal = formal_index.get((case_id, rule_id))
        agreement: bool | None = None

        if not valid:
            comparison_status = output_status
        elif outcome == "undetermined":
            comparison_status = "undetermined"
        elif formal is None:
            comparison_status = "missing_formal_result"
        else:
            agreement = (outcome == "compliance") == bool(formal["formal_satisfied"])
            comparison_status = "agreement" if agreement else "disagreement"

        # Operational failures are not a fourth substantive category or a
        # fabricated model judgement. Valid but non-comparable outputs have
        # an explicit Undetermined comparison, with the raw reason retained.
        category = ("Agreement" if agreement is True else "Disagreement" if agreement is False
                    else "Undetermined") if valid else None
        if not valid:
            reason = output_status
        elif outcome == "undetermined":
            reason = "non_binary_outcome"
        elif formal is None:
            reason = "missing_formal_result"
        else:
            reason = "binary_comparison"

        row.update({
            "case_id": case_id,
            "base_rule_id": rule_id,
            "variant_id": variant_id,
            "llm_output_status": output_status,
            "llm_label_valid": valid,
            "llm_binary_decision": binary,
            "formal_result_available": formal is not None,
            "formal_satisfied": formal["formal_satisfied"] if formal else None,
            "formal_outcome": formal["formal_outcome"] if formal else "unavailable",
            "formal_activation_status": formal["activation_status"] if formal else "unavailable",
            "formal_vacuously_satisfied": formal["vacuously_satisfied"] if formal else None,
            "formal_template": formal.get("template") if formal else None,
            "formal_constraint": formal.get("constraint") if formal else None,
            "formal_trace_sha256": formal.get("trace_sha256") if formal else None,
            "comparison_status": comparison_status,
            "comparison_category": category,
            "comparison_reason": reason,
            "agreement": agreement,
        })
        joined.append(row)
    return joined


def _group_summary(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Expose denominators and preserve the distinction between evaluation units."""
    total = len(rows)
    valid = [row for row in rows if row["llm_label_valid"]]
    binary = [row for row in valid if row["llm_binary_decision"]]
    comparable = [row for row in binary if row["formal_result_available"]]
    agreements = sum(row["agreement"] is True for row in comparable)
    disagreements = sum(row["agreement"] is False for row in comparable)
    status_counts = Counter(str(row["llm_output_status"]) for row in rows)
    not_run = sum(status_counts[status] for status in _NOT_RUN_STATUSES)
    missing = sum(status_counts[status] for status in _MISSING_STATUSES)
    invalid = sum(status_counts[status] for status in _INVALID_STATUSES)
    undetermined = sum(row["outcome"] == "undetermined" for row in valid)
    known_failed = _NOT_RUN_STATUSES | _MISSING_STATUSES | _INVALID_STATUSES | {
        "ok", "error", "api_error", "incomplete", "refusal", "no_text",
    }
    actual_call_flags = [row["call_attempted"] for row in rows if "call_attempted" in row]
    flags_valid = all(isinstance(value, bool) for value in actual_call_flags)
    if not flags_valid:
        raise ValueError("call_attempted must be Boolean when supplied.")
    all_call_flags_available = len(actual_call_flags) == total
    actual_calls = sum(actual_call_flags) if all_call_flags_available else None

    return {
        "evaluation_unit": "case_requirement_variant",
        "n_planned_assessments": total,
        "n_cases": len({row["case_id"] for row in rows}),
        "n_base_rules": len({row["base_rule_id"] for row in rows}),
        "n_rule_variants": len({row["variant_id"] for row in rows}),
        "n_case_rule_pairs": len({(row["case_id"], row["base_rule_id"]) for row in rows}),
        "n_case_variant_pairs": len({(row["case_id"], row["variant_id"]) for row in rows}),
        "n_recorded_results": total - not_run - missing,
        "n_call_attempt_flags_available": len(actual_call_flags),
        "n_calls_attempted": actual_calls,
        "n_valid_outputs": len(valid),
        "n_binary_decisions": len(binary),
        "n_with_formal_result": sum(row["formal_result_available"] for row in rows),
        "n_missing_formal_results": sum(not row["formal_result_available"] for row in rows),
        "n_comparable_binary_decisions": len(comparable),
        "n_agreements": agreements,
        "n_disagreements": disagreements,
        "n_comparison_agreements": agreements,
        "n_comparison_disagreements": disagreements,
        "n_comparison_undetermined": len(valid) - len(comparable),
        "n_undetermined": undetermined,
        "n_pending_or_not_run": not_run,
        "n_missing_outputs": missing,
        "n_invalid_outputs": invalid,
        "n_errors": status_counts["error"] + status_counts["api_error"],
        "n_incomplete": status_counts["incomplete"],
        "n_refusals": status_counts["refusal"],
        "n_no_text": status_counts["no_text"],
        "n_other_failed_outputs": sum(
            count for status, count in status_counts.items() if status not in known_failed
        ),
        "n_formally_vacuous_assessments": sum(
            row["formal_vacuously_satisfied"] is True for row in rows
        ),
        "recorded_result_coverage": _ratio(total - not_run - missing, total),
        "call_attempt_coverage": _ratio(actual_calls, total) if actual_calls is not None else None,
        "valid_output_coverage": _ratio(len(valid), total),
        "binary_decision_coverage": _ratio(len(binary), total),
        "comparison_coverage": _ratio(len(comparable), total),
        "agreement_rate": _ratio(agreements, len(comparable)),
        "disagreement_rate": _ratio(disagreements, len(comparable)),
        "undetermined_rate_among_valid_outputs": _ratio(undetermined, len(valid)),
        "output_status_counts": json.dumps(dict(sorted(status_counts.items())), sort_keys=True),
    }


def _summaries(
    rows: list[Mapping[str, Any]], group_fields: tuple[str, ...],
) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        key = tuple(row.get(field) for field in group_fields)
        groups[key].append(row)
    return [
        {**dict(zip(group_fields, key)), **_group_summary(group)}
        for key, group in groups.items()
    ]


def summarise_comparisons(
    joined: Iterable[Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Summarise every planned assessment without hiding abstentions or failures.

    model_summary separates models, conditions and repetitions. rule_summary
    additionally separates textual variants under their original rule IDs.
    activation_summary keeps formal activation and formal vacuity distinct
    from the LLM's legal decision.

    The confusion matrix includes all planned rows, including unavailable
    outputs and missing formal baselines. It therefore makes the denominator
    of the binary agreement analysis inspectable.
    """
    rows = list(joined)
    required = {
        "case_id", "base_rule_id", "variant_id", "llm_output_status",
        "llm_label_valid", "llm_binary_decision", "formal_result_available",
        "formal_satisfied", "formal_outcome", "formal_activation_status",
        "formal_vacuously_satisfied", "comparison_status", "agreement",
    }
    for row in rows:
        missing = required.difference(row)
        if missing:
            raise ValueError("Use compare_rows before summarising; missing fields: " + ", ".join(sorted(missing)))
    confusion_fields = (
        *_MODEL_FIELDS, "formal_outcome", "formal_activation_status",
        "llm_outcome", "llm_output_status",
    )
    confusion_counts: Counter[tuple[Any, ...]] = Counter()
    for row in rows:
        confusion_row = dict(row)
        confusion_row["llm_outcome"] = row.get("outcome") if row["llm_label_valid"] else ""
        confusion_counts[tuple(confusion_row.get(field) for field in confusion_fields)] += 1

    return {
        "model_summary": _summaries(rows, _MODEL_FIELDS),
        "rule_summary": _summaries(rows, (*_MODEL_FIELDS, "base_rule_id", "variant_id")),
        "activation_summary": _summaries(
            rows, (*_MODEL_FIELDS, "formal_activation_status", "formal_outcome")
        ),
        "confusion_matrix": [
            {**dict(zip(confusion_fields, key)), "n_assessments": count}
            for key, count in confusion_counts.items()
        ],
    }

