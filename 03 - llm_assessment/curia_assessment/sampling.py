"""Select common cases and requirements for a complete crossed assessment.

Rule selection counts original DECLARE rules, not textual variants. Exactly one
supplied formulation of each selected rule is fixed for all selected cases and
models. Random selection among formulations expresses no legal preference.
"""

from __future__ import annotations

import hashlib
import json
import random
from collections import defaultdict
from typing import Any

from .inputs import case_year, requirement_only, select_cases, validate_year_range


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _generator(seed: int, purpose: str, identifier: str = "") -> random.Random:
    """Keep case, rule and individual formulation randomisation independent."""
    material = _canonical([seed, purpose, identifier]).encode("utf-8")
    return random.Random(int.from_bytes(hashlib.sha256(material).digest(), "big"))


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{name} must be a non-empty identifier without surrounding whitespace.")
    return value


def _identifier_list(value: list[str] | None, name: str) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not value:
        raise ValueError(f"{name} must be a non-empty list of unique identifiers or null.")
    values = [_identifier(item, name) for item in value]
    if len(values) != len(set(values)):
        raise ValueError(f"{name} contains duplicate identifiers.")
    return values


def select_cases_by_year(
    cases: list[dict[str, Any]], year_start: int = 2017, year_end: int = 2026,
    cases_per_year: int | None = 5, seed: int = 20261117,
    case_ids: list[str] | None = None, total_cases: int | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Sample annual quotas, then replace shortages from the newest years first.

    The base sample takes up to the quota from every filing year. Compensation
    adds one unused case per year in descending year order, repeating passes
    until the total quota is met or the eligible pool is exhausted. total_cases
    can replace the nominal year-count times quota target, but cannot remove
    cases from the base sample or be combined with explicit/all-case selection.
    Explicit IDs and all-case selections require no compensation.
    """
    validate_year_range(year_start, year_end)
    if cases_per_year is not None and (isinstance(cases_per_year, bool)
            or not isinstance(cases_per_year, int) or (cases_per_year != -1 and cases_per_year < 1)):
        raise ValueError("cases_per_year must be a positive integer, -1 or null.")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("sampling_seed must be an integer.")
    if total_cases is not None and (isinstance(total_cases, bool) or not isinstance(total_cases, int) or total_cases < 1):
        raise ValueError("total_cases must be a positive integer or null for the nominal annual target.")
    if cases_per_year == -1:
        cases_per_year = None
    case_ids = _identifier_list(case_ids, "case_ids")
    if total_cases is not None and case_ids is not None:
        raise ValueError("total_cases cannot be combined with explicit case_ids.")
    if total_cases is not None and cases_per_year is None:
        raise ValueError("total_cases requires a positive cases_per_year quota; do not combine it with -1 or null.")
    # Validate uniqueness even for cases outside the requested range.
    ordered = select_cases(cases, count=None)
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for case in ordered:
        year = case_year(case["case_id"])
        if year_start <= year <= year_end:
            grouped[year].append(case)
    eligible = [case for year in range(year_start, year_end + 1) for case in grouped[year]]
    selected = select_cases(eligible, count=None, case_ids=case_ids) if case_ids is not None else []
    audit = []
    additional_candidates: dict[int, list[dict[str, Any]]] = {}
    for year in range(year_start, year_end + 1):
        available = grouped[year]
        if case_ids is not None:
            chosen = [case for case in selected if case_year(case["case_id"]) == year]
        elif cases_per_year is None or len(available) <= cases_per_year:
            chosen = available
        else:
            chosen = _generator(seed, "case_selection_by_year", str(year)).sample(available, cases_per_year)
        if case_ids is None:
            selected.extend(chosen)
        requested = cases_per_year if case_ids is None else None
        audit.append({
            "year": year, "eligible_cases": len(available),
            "requested_cases": requested, "sampled_cases": len(chosen),
            "base_sampled_cases": len(chosen), "additional_cases": 0,
            "missing_cases": max(0, requested - len(chosen)) if requested is not None else None,
        })
        if case_ids is None and cases_per_year is not None:
            chosen_ids = {case["case_id"] for case in chosen}
            unused = [case for case in available if case["case_id"] not in chosen_ids]
            _generator(seed, "case_shortfall_compensation", str(year)).shuffle(unused)
            additional_candidates[year] = unused
    if case_ids is None and cases_per_year is not None:
        target = total_cases if total_cases is not None else (year_end - year_start + 1) * cases_per_year
        if target < len(selected):
            raise ValueError(f"total_cases={target} is smaller than the annual base sample of {len(selected)} cases; "
                             "increase total_cases or reduce the annual quota/year range.")
        remaining = target - len(selected)
        while remaining:
            added = 0
            for row in reversed(audit):
                unused = additional_candidates[row["year"]]
                if not unused:
                    continue
                selected.append(unused.pop())
                row["additional_cases"] += 1
                row["sampled_cases"] += 1
                remaining -= 1
                added += 1
                if not remaining:
                    break
            if not added:
                break
    if not selected:
        raise ValueError(f"No eligible cases in the CaseID year range {year_start}–{year_end}.")
    if case_ids is None:
        selected.sort(key=lambda case: (case_year(case["case_id"]), case["case_id"]))
    return selected, audit


def select_requirements(
    variants: list[dict[str, Any]],
    rule_count: int | None = None,
    rule_ids: list[str] | None = None,
    variant_ids: list[str] | None = None,
    seed: int = 20261117,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Select distinct base rules and one common formulation for each rule.

    A count of -1 or None selects all eligible original rules.
    Explicit rule IDs override a numeric rule count, as explicit case IDs do in
    case sampling. Explicit variant IDs restrict the available catalogue to the
    named formulations and must name at most one formulation per base rule. If
    both explicit lists are supplied, their base-rule sets must agree exactly.

    Without an explicit formulation, one supplied variant is sampled using an
    independent stream derived from the seed and base-rule ID. Consequently a
    retained rule's formulation does not change when the rule count changes.
    No formal outcome, reviewer score or model output enters this selection.
    """
    if not isinstance(variants, list) or not variants:
        raise ValueError("Requirement selection requires a non-empty variant catalogue.")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("sampling_seed must be an integer.")
    if rule_count is not None and (isinstance(rule_count, bool) or not isinstance(rule_count, int)
                                  or (rule_count != -1 and rule_count < 1)):
        raise ValueError("rule_count must be a positive integer, -1 or null.")
    if rule_count == -1:
        rule_count = None
    rule_ids = _identifier_list(rule_ids, "rule_ids")
    variant_ids = _identifier_list(variant_ids, "variant_ids")

    by_variant: dict[str, dict[str, Any]] = {}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for variant in variants:
        if not isinstance(variant, dict):
            raise ValueError("Each textual variant must be an object.")
        variant = requirement_only(variant)
        variant_id = _identifier(variant.get("variant_id"), "variant_id")
        base_id = _identifier(variant.get("base_rule_id"), "base_rule_id")
        if variant_id in by_variant:
            raise ValueError(f"Duplicate textual variant identifier: {variant_id}.")
        by_variant[variant_id] = variant
        grouped[base_id].append(variant)
    for base_id, members in grouped.items():
        expressions = {member.get("declare") for member in members}
        if len(expressions) > 1:
            raise ValueError(f"Variants of {base_id} contain different DECLARE expressions.")
        members.sort(key=lambda item: item["variant_id"])

    if rule_ids is not None:
        unknown = set(rule_ids) - set(grouped)
        if unknown:
            raise ValueError("Unknown rule identifiers: " + ", ".join(sorted(unknown)))
    explicit_variants: dict[str, dict[str, Any]] = {}
    if variant_ids is not None:
        unknown = set(variant_ids) - set(by_variant)
        if unknown:
            raise ValueError("Unknown variant identifiers: " + ", ".join(sorted(unknown)))
        for variant_id in variant_ids:
            variant = by_variant[variant_id]
            base_id = variant["base_rule_id"]
            if base_id in explicit_variants:
                raise ValueError("Explicit variant_ids must select at most one formulation per original rule.")
            explicit_variants[base_id] = variant
        if rule_ids is not None and set(rule_ids) != set(explicit_variants):
            raise ValueError("rule_ids and variant_ids must identify exactly the same original rules.")

    eligible_ids = sorted(explicit_variants if variant_ids is not None else grouped)
    if rule_ids is not None:
        selected_ids = list(rule_ids)
        rule_method = "explicit_rule_ids"
    elif rule_count is None:
        selected_ids = eligible_ids
        rule_method = "explicit_variant_ids" if variant_ids is not None else "all_available_rules"
    else:
        if rule_count > len(eligible_ids):
            raise ValueError(f"Requested {rule_count} rules, but only {len(eligible_ids)} distinct original rules are available.")
        selected_ids = sorted(_generator(seed, "base_rule_selection").sample(eligible_ids, rule_count))
        rule_method = "seeded_rule_sample"

    chosen: dict[str, dict[str, Any]] = {}
    formulation_methods: dict[str, str] = {}
    for base_id in selected_ids:
        if variant_ids is not None:
            variant = explicit_variants[base_id]
            method = "explicit_variant_id"
        elif len(grouped[base_id]) == 1:
            variant = grouped[base_id][0]
            method = "only_available_variant"
        else:
            variant = _generator(seed, "textual_variant_selection", base_id).choice(grouped[base_id])
            method = "seeded_variant_sample_no_legal_preference"
        chosen[base_id] = dict(variant)
        formulation_methods[base_id] = method

    audit = []
    for base_id in sorted(grouped):
        for variant in grouped[base_id]:
            selected = base_id in chosen and chosen[base_id]["variant_id"] == variant["variant_id"]
            audit.append({
                "base_rule_id": base_id,
                "variant_id": variant["variant_id"],
                "requirement_review_id": variant.get("requirement_review_id", ""),
                "rule_selected": base_id in chosen,
                "variant_selected": selected,
                "available_variant_count": len(grouped[base_id]),
                "rule_selection_method": rule_method,
                "variant_selection_method": formulation_methods.get(base_id, "rule_not_selected"),
                "sampling_seed": seed,
            })
    return [chosen[base_id] for base_id in selected_ids], audit


def cross_pairs(cases: list[dict[str, Any]], selected_variants: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Cross each distinct selected case with every selected original rule."""
    if not isinstance(cases, list) or not cases or not isinstance(selected_variants, list) or not selected_variants:
        raise ValueError("Pair construction requires non-empty lists of cases and selected requirements.")
    case_ids = []
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("Each selected case must be an object.")
        case_ids.append(_identifier(case.get("case_id"), "case_id"))
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("Selected cases contain duplicate case identifiers.")
    base_ids, variant_ids = [], []
    for variant in selected_variants:
        if not isinstance(variant, dict):
            raise ValueError("Each selected requirement must be an object.")
        base_ids.append(_identifier(variant.get("base_rule_id"), "base_rule_id"))
        variant_ids.append(_identifier(variant.get("variant_id"), "variant_id"))
    if len(base_ids) != len(set(base_ids)):
        raise ValueError("Select exactly one textual formulation per original rule before crossing cases and rules.")
    if len(variant_ids) != len(set(variant_ids)):
        raise ValueError("Selected requirements contain duplicate textual variant identifiers.")

    pairs = []
    for case in cases:
        for variant in selected_variants:
            identity = _canonical([case["case_id"], variant["variant_id"]]).encode("utf-8")
            pair = requirement_only({**case, **variant,
                    "pair_id": "pair_" + hashlib.sha256(identity).hexdigest()[:20]})
            pairs.append(pair)
    return pairs
