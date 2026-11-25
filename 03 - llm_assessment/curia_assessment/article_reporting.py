"""Report one phase's descriptive findings without converting missing outputs to law."""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

from .inputs import case_year
from .event_log_statistics import format_event_log_statistics

_OUTCOMES = ("compliance", "violation", "undetermined")
_FIELDS = (
    "phase", "provider_id", "provider", "model", "analysis_view", "n_cases_planned",
    "n_cases_with_complete_valid_response", "n_cases_with_unavailable_rule_judgements",
    "n_cases_with_semantic_undetermined", "n_case_requests_planned",
    "n_case_requests_with_recorded_attempt", "n_api_attempts", "n_technical_failures", "n_recovery_attempts",
    "n_cases_with_recovery", "baseline_output_token_limit", "observed_output_token_limits",
    "n_attempts_above_baseline_output_limit", "n_attempts_with_unknown_output_token_limit",
    "n_rule_judgements_planned", "n_valid_rule_judgements", "n_compliance", "n_violation",
    "n_undetermined", "n_unavailable_rule_judgements", "case_flags_yes",
    "case_flags_no", "case_flags_undetermined", "valid_output_coverage_percent",
    "n_comparison_agreements", "n_comparison_disagreements", "n_comparison_undetermined",
    "formal_agreement_numerator", "formal_agreement_denominator", "formal_agreement_rate",
    "formal_comparison_coverage_percent",
)


def _formal_index(rows: Iterable[Mapping[str, Any]] | None) -> dict[tuple[str, str], int] | None:
    if rows is None:
        return None
    indexed = {}
    for row in rows:
        case_id = row.get("case_id", row.get("CaseID"))
        rule_id = row.get("base_rule_id", row.get("rule_id"))
        value = row.get("formal_satisfied")
        if isinstance(value, str) and value in {"0", "1"}:
            value = int(value)
        if (not isinstance(case_id, str) or not case_id.strip() or not isinstance(rule_id, str) or not rule_id.strip()
                or not isinstance(value, int) or value not in (0, 1)):
            raise ValueError("Article formal results require case/rule identifiers and a binary formal_satisfied state.")
        key = (case_id, rule_id)
        if key in indexed:
            raise ValueError("Article formal results contain a duplicate case–rule pair.")
        indexed[key] = int(value)
    return indexed


def _summarise(
    group: list[dict[str, Any]], pairs: dict[str, dict[str, Any]],
    histories: Mapping[str, list[dict[str, Any]]], view: str, baseline_limit: int,
    formal: dict[tuple[str, str], int] | None, phase: int,
) -> dict[str, Any]:
    provider = group[0]
    outcomes = Counter()
    case_results: dict[str, list[tuple[bool, list[str | None]]]] = defaultdict(list)
    planned = attempted = n_attempts = recoveries = unknown_limits = changed_limits = 0
    recovered_cases = set()
    observed_limits = set()
    agreements = comparable = technical_failures = 0
    for job in group:
        history = histories.get(job["job_id"], [])
        if not isinstance(history, list) or any(not isinstance(record, dict) for record in history):
            raise ValueError("Article attempts must be ordered lists of recorded attempt objects.")
        visible = history[:1] if view == "first_attempt" else history
        result = visible[-1] if visible else {}
        attempted += bool(visible)
        n_attempts += len(visible)
        recoveries += max(0, len(visible) - 1)
        if len(visible) > 1:
            recovered_cases.add(job["case_id"])
        for record in visible:
            limit = record.get("actual_output_token_limit")
            if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
                unknown_limits += 1
            else:
                observed_limits.add(limit)
                changed_limits += limit > baseline_limit
        members = [pairs[identifier] for identifier in job["pair_ids"]]
        expected = [pair["base_rule_id"] for pair in members]
        if not expected or len(expected) != len(set(expected)):
            raise ValueError("Each article case request must contain distinct selected rules.")
        if any(pair["case_id"] != job["case_id"] for pair in members):
            raise ValueError("An article case request contains another case's rule pair.")
        supplied: dict[str, list[dict]] = defaultdict(list)
        records = result.get("assessments", [])
        if not isinstance(records, list):
            records = []
        for record in records:
            if isinstance(record, dict) and isinstance(record.get("rule_id"), str):
                supplied[record["rule_id"]].append(record)
        valid_outcomes: list[str | None] = []
        for rule_id in expected:
            planned += 1
            candidates = supplied[rule_id]
            record = candidates[0] if len(candidates) == 1 else {}
            outcome = record.get("outcome") if record.get("status") == "ok" else None
            outcome = outcome if outcome in _OUTCOMES else None
            valid_outcomes.append(outcome)
            if outcome is None:
                continue
            outcomes[outcome] += 1
            if outcome in {"compliance", "violation"} and formal is not None:
                state = formal.get((job["case_id"], rule_id))
                if state is not None:
                    comparable += 1
                    agreements += (outcome == "compliance") == bool(state)
        complete = (result.get("status") == "ok" and all(value is not None for value in valid_outcomes)
                    and set(supplied) == set(expected))
        technical_failures += bool(visible) and not complete
        case_results[job["case_id"]].append((complete, valid_outcomes))
    flags = Counter()
    complete_cases = 0
    unavailable_cases = semantic_undetermined_cases = 0
    for records in case_results.values():
        complete = all(record[0] for record in records)
        complete_cases += complete
        decisions = [outcome for _, values in records for outcome in values]
        unavailable_cases += None in decisions
        semantic_undetermined_cases += "undetermined" in decisions
        if "violation" in decisions:
            flags["yes"] += 1
        elif complete and all(outcome == "compliance" for outcome in decisions):
            flags["no"] += 1
        else:
            flags["undetermined"] += 1
    valid = sum(outcomes.values())
    return {
        "phase": phase,
        "provider_id": provider["provider_id"], "provider": provider["provider"], "model": provider["model"],
        "analysis_view": view, "n_cases_planned": len(case_results),
        "n_cases_with_complete_valid_response": complete_cases,
        "n_cases_with_unavailable_rule_judgements": unavailable_cases,
        "n_cases_with_semantic_undetermined": semantic_undetermined_cases,
        "n_case_requests_planned": len(group),
        "n_case_requests_with_recorded_attempt": attempted, "n_api_attempts": n_attempts,
        "n_technical_failures": technical_failures,
        "n_recovery_attempts": recoveries, "n_cases_with_recovery": len(recovered_cases),
        "baseline_output_token_limit": baseline_limit,
        "observed_output_token_limits": json.dumps(sorted(observed_limits)),
        "n_attempts_above_baseline_output_limit": changed_limits,
        "n_attempts_with_unknown_output_token_limit": unknown_limits,
        "n_rule_judgements_planned": planned, "n_valid_rule_judgements": valid,
        **{"n_" + outcome: outcomes[outcome] for outcome in _OUTCOMES},
        "n_unavailable_rule_judgements": planned - valid,
        "case_flags_yes": flags["yes"], "case_flags_no": flags["no"],
        "case_flags_undetermined": flags["undetermined"],
        "valid_output_coverage_percent": 100 * valid / planned if planned else None,
        "n_comparison_agreements": agreements,
        "n_comparison_disagreements": comparable - agreements,
        "n_comparison_undetermined": valid - comparable,
        "formal_agreement_numerator": agreements if formal is not None else None,
        "formal_agreement_denominator": comparable if formal is not None else None,
        "formal_agreement_rate": agreements / comparable if comparable else None,
        "formal_comparison_coverage_percent": 100 * comparable / planned if formal is not None and planned else None,
    }


def _results_paragraph(rows: list[dict[str, Any]], title: str, *, uniform_budget: bool = False) -> str:
    parts = [title]
    for row in rows:
        coverage = row["valid_output_coverage_percent"]
        statement = (
            f"{row['provider']} ({row['model']}): {row['n_cases_with_complete_valid_response']}/"
            f"{row['n_cases_planned']} cases had a complete schema-valid response; "
            f"{row['n_valid_rule_judgements']}/{row['n_rule_judgements_planned']} rule judgements met "
            f"output-validation requirements ({coverage:.1f}% coverage). The formal comparison comprised "
            f"Agreement: {row['n_comparison_agreements']}, "
            f"Disagreement: {row['n_comparison_disagreements']} and "
            f"Undetermined: {row['n_comparison_undetermined']}; "
            f"{row['n_unavailable_rule_judgements']} planned judgements were operationally unavailable. "
            f"Aggregated case flags were yes={row['case_flags_yes']}, no={row['case_flags_no']} and "
            f"undetermined={row['case_flags_undetermined']}. "
            f"{row['n_cases_with_unavailable_rule_judgements']} cases contained operationally unavailable rule "
            f"judgements and {row['n_cases_with_semantic_undetermined']} contained a parsed legal undetermined "
            "judgement; these two counts may overlap."
        )
        if row["formal_agreement_denominator"] is None:
            statement += " No formal reference was supplied to this report; formal agreement is unavailable."
        elif row["formal_agreement_denominator"]:
            statement += (
                f" Agreement with the archived formal checker was {row['formal_agreement_numerator']}/"
                f"{row['formal_agreement_denominator']} comparable binary judgements "
                f"({100 * row['formal_agreement_rate']:.1f}%)."
            )
        else:
            statement += " No output-validated binary judgement could be compared with the archived formal reference."
        if row["formal_comparison_coverage_percent"] is not None:
            statement += (f" Formal-comparison coverage was {row['formal_agreement_denominator']}/"
                          f"{row['n_rule_judgements_planned']} planned rule judgements "
                          f"({row['formal_comparison_coverage_percent']:.1f}%); agreement is conditional on that denominator.")
        statement += f" The selected responses contained {row['n_technical_failures']} technical failures."
        statement += f" This view accounts for {row['n_api_attempts']} recorded API attempts."
        if row["analysis_view"] == "latest":
            statement += (f" It includes {row['n_recovery_attempts']} recovery attempts across "
                          f"{row['n_cases_with_recovery']} cases.")
            if uniform_budget:
                statement += f" The cohort's configured output-token limit was {row['baseline_output_token_limit']}."
            else:
                statement += (f" {row['n_attempts_above_baseline_output_limit']} attempts used a higher output-token limit "
                              f"than the configured baseline of {row['baseline_output_token_limit']}.")
        if row["n_attempts_with_unknown_output_token_limit"]:
            statement += (f" The actual output-token limit was unavailable for "
                          f"{row['n_attempts_with_unknown_output_token_limit']} retained attempts.")
        parts.append(statement)
    return " ".join(parts)


def _summary(
    config: Mapping[str, Any], pairs: dict[str, dict], jobs: list[dict], rows: list[dict], phase: int,
    event_log_statistics: Mapping[str, Any] | None = None,
) -> str:
    cases = sorted({job["case_id"] for job in jobs})
    rules = {pairs[pair_id]["base_rule_id"] for job in jobs for pair_id in job["pair_ids"]}
    models = {(job["provider_id"], job["model"]) for job in jobs}
    baseline = [row for row in rows if row["analysis_view"] == "first_attempt"]
    latest = [row for row in rows if row["analysis_view"] == "latest"]
    latest_only = config.get("article_report_view", "first_and_latest") == "latest"
    planned = sum(row["n_case_requests_planned"] for row in baseline)
    attempted = sum(row["n_case_requests_with_recorded_attempt"] for row in baseline)
    state = (f"Interim results: {attempted}/{planned} planned case requests have a recorded attempt. "
             "Unattempted requests remain pending; planned rows are not model findings."
             if attempted < planned else
             "Every planned case request has a recorded attempt. Valid-output coverage below 100% indicates "
             "remaining operationally unavailable judgements, not additional legal undetermined outcomes.")
    country = config.get("country")
    documents = (
        "Case documents were not supplied to the models in this phase; "
        "availability of at least one local document was an eligibility condition. "
        if phase == 1 else
        "Phase 2 used the same cases, textual requirements, original DECLARE formulas and ordered event traces as phase 1, "
        "adding the full frozen case-document packets. No formal-checker results or model answers from "
        "phase 1 were supplied to the models. "
    )
    methods = (
        f"Methods. The selected sample comprised {len(cases)} distinct CaseID clusters and {len(rules)} supplied "
        f"procedural requirements, crossed with {len(models)} configured models. The phase-{phase} allocation planned "
        f"{planned} case requests and {sum(row['n_rule_judgements_planned'] for row in baseline)} rule judgements. "
        "Each planned case request contained all selected textual requirements and their original DECLARE formulas and "
        "instructed separate judgements using the same complete ordered event trace. "
        f"{documents}"
        f"The recorded country filter was {country!r}; the minimum recorded event count was "
        f"{config.get('min_events', 'unavailable')}. Case years were taken from the case number, rather than event dates. "
        f"Annual sampling used seed {config.get('sampling_seed', 'unavailable')} and a configured quota of "
        f"{config.get('cases_per_year', 'unavailable')} case(s) per year."
    )
    years = Counter(case_year(case_id) for case_id in cases)
    quota = config.get("cases_per_year")
    first, last = config.get("year_start"), config.get("year_end")
    if (isinstance(quota, int) and not isinstance(quota, bool) and quota > 0
            and config.get("case_ids") is None
            and all(isinstance(year, int) and not isinstance(year, bool) for year in (first, last))
            and first <= last and all(first <= year <= last for year in years)):
        window = range(first, last + 1)
        nominal = len(window) * quota
        target_override = config.get("total_cases")
        target = nominal if target_override is None else target_override
        base = sum(min(years[year], quota) for year in window)
        deficits = {year: quota - years[year] for year in window if years[year] < quota}
        additions = {year: years[year] - quota for year in window if years[year] > quota}
        added = sum(additions.values())
        if target_override is None:
            methods += (
                f" The inclusive {first}–{last} sampling window gave a target of {target} distinct "
                f"{'case' if target == 1 else 'cases'}."
            )
        else:
            methods += (
                f" The inclusive {first}–{last} sampling window and annual quota gave a nominal total of {nominal} "
                f"{'case' if nominal == 1 else 'cases'}."
                f" The explicit total_cases setting specified an overall target of {target} distinct "
                f"{'case' if target == 1 else 'cases'}, overriding that nominal total."
            )
        methods += f" The base annual selection contained {base} {'case' if base == 1 else 'cases'}."
        if deficits:
            methods += (
                f" Unfilled base-quota places totalled {sum(deficits.values())}"
                " (missing places by CaseID year: "
                + "; ".join(f"{year}: {count}" for year, count in deficits.items()) + ")."
            )
            methods += (
                f" Compensation added {added} {'case' if added == 1 else 'cases'}"
                + (" (additional cases by CaseID year: "
                   + "; ".join(f"{year}: {count}" for year, count in additions.items()) + ")"
                   if additions else "")
                + ", cycling from the latest year backwards until the target was met or the eligible pool was exhausted."
            )
        else:
            methods += " Every annual base quota was filled; no compensation was needed."
        methods += (
            " Final selected cases by CaseID year were "
            + "; ".join(f"{year}: {years[year]}" for year in window) + "."
            + f" The final sample contained {len(cases)}/{target} target cases,"
            + f" leaving an overall shortfall of {max(0, target - len(cases))}."
        )
    if latest_only:
        methods += (
            " The report uses the latest archived response for each planned case request in this cohort. "
            f"Every planned request has the same configured output-token limit of {config['max_output_tokens']}. "
            "API attempt counts include every retained attempt in this cohort, including unsuccessful technical "
            "attempts; a recovery response supersedes its preceding response for outcome reporting. "
        )
    else:
        methods += (
            " The first-attempt view uses the first archived response for each planned case request; the latest view uses "
            "the most recent response, including explicit operational recoveries. First-attempt API counts include only "
            "the first retained attempt per request; latest-view API counts include every retained attempt, including "
            "unsuccessful attempts. "
        )
    methods += (
        "A recorded attempt does not by itself establish a completed generation. "
        "Unknown or failed outputs remain operationally unavailable and are not recoded as "
        "legal undetermined decisions. Output validation checks the JSON structure, allocation to all requested "
        "rule identifiers, allowed outcome labels and allowed evidence references. Passing these checks does "
        "not establish that a judgement is legally correct or that its evidence supports the conclusion. "
        "Technical failures count attempted case responses that are incomplete or fail output validation; "
        "unattempted requests are pending, and a schema-valid legal undetermined judgement is not a technical failure. "
        "A case flag is yes if any output-validated rule judgement reports a violation, no only if every planned "
        "rule judgement passes output validation and reports compliance, and undetermined otherwise."
    )
    limitations = (
        f"Limitations. The {len(rules)} rule judgements within each case are dependent; the sample contains "
        f"{len(cases)} case clusters, rather than {len(cases) * len(rules)} independent observations per model. "
        "The design uses one case draw and one intended generation per case and provider; recovery attempts are "
        "operational attempts, not independent experimental repetitions. "
        + ("The published findings concern only this cohort and its configured output budget. " if latest_only else
           "Later responses obtained with a higher output-token limit belong to the latest-response sensitivity view "
           "and must not replace the original first-attempt findings. ")
        + "Document-availability selection and the recorded country and event-count filters "
        "limit generalisation. These descriptive results do not establish population-wide violation rates or legal "
        "accuracy. The formal comparison uses Agreement, Disagreement and Undetermined. Valid non-binary "
        "model responses and valid responses without an available checker state enter comparison Undetermined; "
        "operationally unavailable judgements remain separate. The conditional agreement denominator includes "
        "only Agreement and Disagreement. Independent review by two experts has not been incorporated into these results; "
        "no expert ground-truth labels or inter-rater estimates are reported."
    )
    paragraphs = [state, methods]
    if event_log_statistics is not None:
        paragraphs.append(format_event_log_statistics(event_log_statistics))
    if not latest_only:
        paragraphs.append(_results_paragraph(baseline, "Results, first attempts."))
    paragraphs.append(_results_paragraph(latest, "Results, latest responses.", uniform_budget=latest_only))
    paragraphs.append(limitations)
    return "\n\n".join(paragraphs) + "\n"


def write_article_outputs(
    output: str | Path, *, config: Mapping[str, Any], pairs: Mapping[str, dict] | Iterable[dict],
    jobs: Iterable[dict], attempts_by_job: Mapping[str, list[dict]],
    formal_rows: Iterable[Mapping[str, Any]] | None = None,
    event_log_statistics: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Write one phase's first/latest descriptive counts and manuscript-ready prose.

    Recorded attempts must retain chronological order. No response is selected
    for being more favourable or for agreeing with the checker. Only jobs from
    the single configured phase are included; the default phase is A.
    """
    conditions = config.get("conditions", ["A"])
    if not isinstance(conditions, (list, tuple)) or len(conditions) != 1 or conditions[0] not in ("A", "B"):
        raise ValueError("Article reporting requires exactly one configured phase: A or B.")
    condition = conditions[0]
    phase = 1 if condition == "A" else 2
    catalogue = list(pairs.values()) if isinstance(pairs, Mapping) else list(pairs)
    indexed_pairs = {pair["pair_id"]: pair for pair in catalogue}
    if len(indexed_pairs) != len(catalogue):
        raise ValueError("Article reporting requires unique pair identifiers.")
    planned = [job for job in jobs if job.get("condition", "A") == condition]
    if not planned:
        raise ValueError(f"Article reporting requires planned phase-{phase} case requests.")
    if len({job["job_id"] for job in planned}) != len(planned):
        raise ValueError("Article reporting requires unique case-request identifiers.")
    if any(pair_id not in indexed_pairs for job in planned for pair_id in job["pair_ids"]):
        raise ValueError("An article case request refers to an unknown case–rule pair.")
    baseline_limit = config.get("max_output_tokens")
    if isinstance(baseline_limit, bool) or not isinstance(baseline_limit, int) or baseline_limit < 1:
        raise ValueError("Article reporting requires the positive baseline max_output_tokens setting.")
    target_override = config.get("total_cases")
    if target_override is not None and (isinstance(target_override, bool)
            or not isinstance(target_override, int) or target_override < 1):
        raise ValueError("Article reporting requires total_cases to be a positive integer or null.")
    report_view = config.get("article_report_view", "first_and_latest")
    if report_view not in {"first_and_latest", "latest"}:
        raise ValueError("article_report_view must be first_and_latest or latest.")
    formal = _formal_index(formal_rows)
    groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for job in planned:
        groups[(job["provider_id"], job["provider"], job["model"])].append(job)
    all_rows = [_summarise(groups[key], indexed_pairs, attempts_by_job, view, baseline_limit, formal, phase)
                for key in sorted(groups) for view in ("first_attempt", "latest")]
    rows = [row for row in all_rows if report_view != "latest" or row["analysis_view"] == "latest"]
    if report_view == "latest" and any(limit != baseline_limit for row in rows
                                       for limit in json.loads(row["observed_output_token_limits"])):
        raise ValueError("The latest article view requires an output-budget-consistent cohort matching max_output_tokens.")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    csv_path = output / "article_results.csv"
    temporary_csv = csv_path.with_name(csv_path.name + ".tmp")
    with temporary_csv.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    temporary_csv.replace(csv_path)
    text_path = output / "article_summary.txt"
    temporary_text = text_path.with_name(text_path.name + ".tmp")
    temporary_text.write_text(_summary(config, indexed_pairs, planned, all_rows, phase,
                                      event_log_statistics), encoding="utf-8")
    temporary_text.replace(text_path)
    return {"article_results": csv_path.name, "article_summary": text_path.name}
