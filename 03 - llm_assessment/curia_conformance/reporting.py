"""Summarise formal DECLARE results without conflating vacuity and violation.

All scores describe the supplied rule catalogue and recorded traces. They do not
establish legal applicability, evidential sufficiency or legal compliance.

For a group of ``P`` case--rule pairs, let ``S`` be the number satisfied
non-vacuously and ``I`` the number satisfied vacuously. The standard score is
``(S + I) / P``. The positive-evidence score is ``S / P`` and the
vacuity-excluded score is ``S / (P - I)``. Here "positive evidence" means a
non-vacuous formal result, not independent legal evidence. Direct constraints,
such as Existence and Init, remain in all three denominators and are never
discarded merely because an event-triggered activation count is inapplicable.

The event-triggered conditional score is a separate measure: the proportion of
formally satisfied pairs among pairs explicitly marked ``active``. Undefined
ratios remain missing, including groups with no active or non-vacuous pairs.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
import itertools
import json
import math
import re
from typing import Any

import pandas as pd


_REQUIRED_COLUMNS = (
    "CaseID",
    "rule_id",
    "constraint",
    "template",
    "formal_satisfied",
    "formal_outcome",
    "activation_status",
    "vacuously_satisfied",
)
_COUNT_COLUMNS = ("activation_count", "fulfilment_count", "violation_count")
_OUTCOMES = {"satisfied", "violated", "vacuously_satisfied"}
_ACTIVATION_STATUSES = {"active", "inactive", "not_applicable"}


def _validated_pairs(pairs: pd.DataFrame) -> pd.DataFrame:
    """Validate a complete case--rule table, preserving its order and columns."""
    missing = [column for column in _REQUIRED_COLUMNS if column not in pairs]
    if missing:
        raise ValueError(f"The result table is missing columns: {', '.join(missing)}")
    if pairs.empty:
        raise ValueError("Cannot summarise an empty case--rule result table.")
    if pairs.columns.duplicated().any():
        raise ValueError("The result table contains duplicate column names.")

    frame = pairs.copy(deep=True)
    for column in ("CaseID", "rule_id", "constraint", "template"):
        valid = frame[column].map(lambda value: isinstance(value, str) and bool(value.strip()))
        if not valid.all():
            raise ValueError(f"Column {column!r} must contain non-empty strings.")

    if frame.duplicated(["CaseID", "rule_id"]).any():
        raise ValueError("Each CaseID--rule_id pair must occur exactly once.")
    expected_pairs = frame["CaseID"].nunique() * frame["rule_id"].nunique()
    if len(frame) != expected_pairs:
        raise ValueError(
            "The case--rule table is incomplete. Missing evaluations must not "
            "be silently treated as satisfaction or excluded from denominators."
        )

    for column in ("constraint", "template"):
        if frame.groupby("rule_id", sort=False)[column].nunique(dropna=False).gt(1).any():
            raise ValueError(f"Each rule_id must have one consistent {column} value.")

    try:
        state = pd.to_numeric(frame["formal_satisfied"], errors="raise")
    except (TypeError, ValueError) as exc:
        raise ValueError("formal_satisfied must contain only 0 or 1.") from exc
    if state.isna().any() or not state.isin([0, 1]).all():
        raise ValueError("formal_satisfied must contain only 0 or 1; missing states are invalid.")
    frame["formal_satisfied"] = state.astype(int)

    if not frame["formal_outcome"].isin(_OUTCOMES).all():
        raise ValueError("formal_outcome contains an unknown or missing outcome.")
    if not frame["activation_status"].isin(_ACTIVATION_STATUSES).all():
        raise ValueError("activation_status contains an unknown or missing status.")
    if frame["vacuously_satisfied"].isna().any() or not frame["vacuously_satisfied"].isin(
        [True, False]
    ).all():
        raise ValueError("vacuously_satisfied must contain Boolean values.")
    frame["vacuously_satisfied"] = frame["vacuously_satisfied"].astype(bool)

    expected_satisfied = frame["formal_outcome"].ne("violated").astype(int)
    expected_vacuous = frame["formal_outcome"].eq("vacuously_satisfied")
    if not frame["formal_satisfied"].eq(expected_satisfied).all():
        raise ValueError("formal_satisfied disagrees with formal_outcome.")
    if not frame["vacuously_satisfied"].eq(expected_vacuous).all():
        raise ValueError("vacuously_satisfied disagrees with formal_outcome.")
    if (
        frame["activation_status"].eq("not_applicable")
        & frame["vacuously_satisfied"]
    ).any():
        raise ValueError("A direct constraint must not be classified as vacuously satisfied.")
    if (frame["activation_status"].eq("active") & frame["vacuously_satisfied"]).any():
        raise ValueError("An active pair must not be classified as vacuously satisfied.")

    for column in _COUNT_COLUMNS:
        if column not in frame:
            frame[column] = pd.Series(pd.NA, index=frame.index, dtype="Int64")
            continue
        try:
            counts = pd.to_numeric(frame[column], errors="raise")
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{column} must contain non-negative integers or missing values.") from exc
        available = counts.dropna()
        if not available.map(lambda value: math.isfinite(value) and value >= 0 and value % 1 == 0).all():
            raise ValueError(f"{column} must contain non-negative integers or missing values.")
        frame[column] = counts.astype("Int64")

    active = frame["activation_status"].eq("active")
    inactive = frame["activation_status"].eq("inactive")
    counts_available = frame["activation_count"].notna()
    if (active & counts_available & frame["activation_count"].le(0)).any():
        raise ValueError("An active pair must have a positive activation count when supplied.")
    if (inactive & counts_available & frame["activation_count"].ne(0)).any():
        raise ValueError("An inactive pair must have zero activations when a count is supplied.")
    return frame


def _ratio(numerator: int | float, denominator: int | float) -> float:
    """Return a full-precision ratio, leaving empty denominators undefined."""
    return float(numerator / denominator) if denominator else math.nan


def _group_summary(group: pd.DataFrame) -> dict[str, int | float | None]:
    """Count disjoint outcomes and retain each score's denominator explicitly."""
    total = len(group)
    satisfied = int(group["formal_satisfied"].sum())
    vacuous = int(group["vacuously_satisfied"].sum())
    non_vacuous_satisfied = satisfied - vacuous
    violated = total - satisfied
    active = group["activation_status"].eq("active")
    inactive = group["activation_status"].eq("inactive")
    direct = group["activation_status"].eq("not_applicable")
    n_active = int(active.sum())
    n_inactive = int(inactive.sum())
    n_direct = int(direct.sum())
    active_satisfied = int(group.loc[active, "formal_satisfied"].sum())
    direct_satisfied = int(group.loc[direct, "formal_satisfied"].sum())
    result: dict[str, int | float | None] = {
        "n_pairs": total,
        "n_formally_satisfied": satisfied,
        "n_satisfied_non_vacuously": non_vacuous_satisfied,
        "n_vacuously_satisfied": vacuous,
        "n_formally_violated": violated,
        "n_non_vacuous_pairs": total - vacuous,
        "n_active_pairs": n_active,
        "n_inactive_pairs": n_inactive,
        "n_event_triggered_pairs": n_active + n_inactive,
        "n_direct_pairs": n_direct,
        "n_active_satisfied_pairs": active_satisfied,
        "n_active_violated_pairs": n_active - active_satisfied,
        "n_direct_satisfied_pairs": direct_satisfied,
        "n_direct_violated_pairs": n_direct - direct_satisfied,
        "standard_conformance_score": _ratio(satisfied, total),
        "formal_violation_rate": _ratio(violated, total),
        "vacuity_rate": _ratio(vacuous, total),
        "positive_evidence_score": _ratio(non_vacuous_satisfied, total),
        "vacuity_excluded_score": _ratio(non_vacuous_satisfied, total - vacuous),
        "activation_rate": _ratio(n_active, n_active + n_inactive),
        "event_triggered_conditional_score": _ratio(active_satisfied, n_active),
        "direct_constraint_score": _ratio(direct_satisfied, n_direct),
    }
    for source, total_name, coverage_name in (
        ("activation_count", "total_activations", "pairs_with_activation_counts"),
        ("fulfilment_count", "total_fulfilments", "pairs_with_fulfilment_counts"),
        ("violation_count", "total_activation_violations", "pairs_with_violation_counts"),
    ):
        available = group[source].dropna()
        result[total_name] = int(available.sum()) if len(available) else None
        result[coverage_name] = len(available)
    return result


def summarise_results(pairs: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Produce summaries and matrices from a complete case--rule result table.

    Required fields are CaseID, rule_id, constraint, template,
    formal_satisfied (0/1), formal_outcome (satisfied, violated or
    vacuously_satisfied), activation_status (active, inactive or
    not_applicable), and vacuously_satisfied (Boolean). Optional activation,
    fulfilment and violation counts are nullable non-negative integers.

    ``not_applicable`` concerns the activation concept for direct constraints;
    it never means that a legal requirement is inapplicable to the case.

    Matrices contain CaseID as a regular first column so they can be exported
    with ``index=False``. All tables preserve the first-seen case and rule
    order, except rule_impact, which is ordered by descending impact, and the
    violation distribution, which is ordered by violation count.

    Every global score is a ratio over the pooled case--rule pairs. It is not
    an unweighted average of case-level conditional scores with varying
    denominators. The standard score equals the mean case score because a
    complete rectangular case--rule table is required.

    The leave-one-rule-out delta is descriptive. For m > 1 it is exactly
    (rule_violation_rate - mean_rule_violation_rate) / (m - 1), so its ordering
    is the same as the ordering by violation rate. No causal or legal
    importance is implied. With only one rule, the comparison is undefined.
    """
    frame = _validated_pairs(pairs)
    case_order = list(frame["CaseID"].drop_duplicates())
    rule_order = list(frame["rule_id"].drop_duplicates())
    n_cases = len(case_order)
    n_rules = len(rule_order)

    rule_rows: list[dict[str, Any]] = []
    for rule_id, group in frame.groupby("rule_id", sort=False):
        rule_rows.append(
            {
                "rule_id": rule_id,
                "constraint": group["constraint"].iloc[0],
                "template": group["template"].iloc[0],
                "n_cases": len(group),
                **_group_summary(group),
            }
        )
    rule_summary = pd.DataFrame(rule_rows)

    case_rows = [
        {"CaseID": case_id, "n_rules": len(group), **_group_summary(group)}
        for case_id, group in frame.groupby("CaseID", sort=False)
    ]
    case_summary = pd.DataFrame(case_rows)
    global_summary = pd.DataFrame(
        [{"n_cases": n_cases, "n_rules": n_rules, **_group_summary(frame)}]
    )

    satisfaction = frame.pivot(index="CaseID", columns="rule_id", values="formal_satisfied")
    satisfaction = satisfaction.reindex(index=case_order, columns=rule_order).astype(int)
    satisfaction.columns.name = None
    violation = 1 - satisfaction
    satisfaction_matrix = satisfaction.reset_index()
    violation_matrix = violation.reset_index()

    baseline = float(global_summary["standard_conformance_score"].iloc[0])
    mean_violation = float(global_summary["formal_violation_rate"].iloc[0])
    impact_rows: list[dict[str, Any]] = []
    for row in rule_rows:
        delta = (float(row["formal_violation_rate"]) - mean_violation) / (n_rules - 1) if n_rules > 1 else math.nan
        impact_rows.append(
            {
                "rule_id": row["rule_id"],
                "constraint": row["constraint"],
                "template": row["template"],
                "n_cases": n_cases,
                "n_rules": n_rules,
                "n_formally_violated": row["n_formally_violated"],
                "formal_violation_rate": row["formal_violation_rate"],
                "baseline_conformance_score": baseline,
                "conformance_without_rule": baseline + delta,
                "impact_delta": delta,
            }
        )
    rule_impact = pd.DataFrame(impact_rows)
    rule_impact.insert(
        0,
        "impact_rank",
        rule_impact["impact_delta"].rank(method="min", ascending=False).astype("Int64"),
    )
    rule_impact = rule_impact.sort_values(
        "impact_delta", ascending=False, kind="stable", na_position="last"
    ).reset_index(drop=True)

    distribution = (
        case_summary["n_formally_violated"]
        .value_counts()
        .sort_index()
        .rename_axis("n_formally_violated")
        .reset_index(name="n_cases")
    )
    distribution["case_fraction"] = distribution["n_cases"] / n_cases

    return {
        "rule_summary": rule_summary,
        "case_summary": case_summary,
        "global_summary": global_summary,
        "rule_impact": rule_impact,
        "violation_distribution": distribution,
        "satisfaction_matrix": satisfaction_matrix,
        "violation_matrix": violation_matrix,
    }


def _field(rule: object, name: str, default: Any = None) -> Any:
    """Read lightweight rule records without depending on a particular parser."""
    return rule.get(name, default) if isinstance(rule, Mapping) else getattr(rule, name, default)


def _template_key(template: object) -> str:
    """Normalise spelling only for the small catalogue diagnostic below."""
    return re.sub(r"[\s_-]", "", str(template)).lower()


def diagnose_catalogue(rules: Iterable[object]) -> pd.DataFrame:
    """Identify provable presence/exclusion conflicts under a shared scope.

    The diagnostic recognises positive Existence/AtLeastOne requirements for
    two activities together with NotCoExistence for those activities. Rules
    with explicit non-empty data/time conditions are skipped. Results concern
    the hypothetical interpretation in which the listed rules apply to the
    same trace; different legal scopes may explain the conflict. This is not
    a complete satisfiability test and never removes or changes a rule.

    Each rule may be a mapping or an object exposing rule_id, template and
    activities. Native Existence cardinalities greater than zero also imply
    presence, so they can support this narrow diagnostic.
    """
    columns = ["diagnostic_id", "severity", "diagnostic_type", "rule_ids", "activities", "explanation"]
    existence: defaultdict[str, list[str]] = defaultdict(list)
    exclusions: list[tuple[str, tuple[str, ...]]] = []
    for rule in rules:
        conditions = _field(rule, "conditions", _field(rule, "condition", ()))
        if isinstance(conditions, str):
            conditions = (conditions,)
        if conditions and any(str(value).strip() for value in conditions if value is not None):
            continue
        rule_id = str(_field(rule, "rule_id", ""))
        template = _template_key(_field(rule, "template", ""))
        activities = tuple(_field(rule, "activities", ()))
        if not rule_id:
            raise ValueError("Each catalogue rule must have a rule_id.")
        existence_match = re.fullmatch(r"existence([1-9][0-9]*)?", template)
        if len(activities) == 1 and (template == "atleastone" or existence_match):
            existence[str(activities[0])].append(rule_id)
        elif len(activities) == 2 and template == "notcoexistence":
            exclusions.append((rule_id, activities))

    rows: list[dict[str, str]] = []
    seen: set[tuple[str, ...]] = set()
    for exclusion_id, activities in exclusions:
        first, second = map(str, activities)
        for first_id, second_id in itertools.product(existence[first], existence[second]):
            rule_ids = tuple(dict.fromkeys((first_id, second_id, exclusion_id)))
            if rule_ids in seen:
                continue
            seen.add(rule_ids)
            rows.append(
                {
                    "diagnostic_id": f"D{len(rows) + 1:03d}",
                    "severity": "conflicting_if_same_scope",
                    "diagnostic_type": "inconsistent_under_shared_scope",
                    "rule_ids": json.dumps(rule_ids, ensure_ascii=False),
                    "activities": json.dumps((first, second), ensure_ascii=False),
                    "explanation": (
                        "If these rules apply to the same trace, the existence "
                        "requirements demand both activities while NotCoExistence "
                        "prohibits their co-occurrence. Retain the rules and check "
                        "their applicability scopes separately."
                    ),
                }
            )
    return pd.DataFrame(rows, columns=columns)
