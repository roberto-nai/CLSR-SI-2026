"""Select a fresh cohort offline using verified DECLARE activation metadata."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime
from fractions import Fraction
import hashlib
from itertools import combinations
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from curia_assessment import formal, inputs, pipeline as p

DEFAULT_CONFIG = "config.phase1.50_cases.32000.three_outcomes.frozen.json"
DEFAULT_BASELINE = "outputs/phase1_2016_2026_50_cases_32000_three_outcomes_20261123"
BASELINE_MODEL_CODE_SHA256 = "50e3672e666ee81cf4b57709f473dc068c3cb632c990d3db3afb1c5fa60d06da"


def _natural(value: str) -> tuple:
    return tuple((1, int(part)) if part.isdigit() else (0, part)
                 for part in re.split(r"(\d+)", value))


def _order(case: dict) -> tuple:
    return (case.get("case_year", inputs.case_year(case["case_id"])), _natural(case["case_id"]))


def build_profiles(cases: list[dict], formal: list[dict]) -> list[dict]:
    """Join complete case/rule pairs; count distinct rules, never activations."""
    lookup = {case["case_id"]: case for case in cases}
    if not lookup or len(lookup) != len(cases):
        raise ValueError("Cases must be non-empty and have unique identifiers.")
    grouped: dict[str, dict[str, dict]] = defaultdict(dict)
    kinds: dict[str, set[bool]] = defaultdict(set)
    for row in formal:
        case_id = row.get("case_id", row.get("CaseID"))
        rule = row.get("base_rule_id", row.get("rule_id"))
        if case_id not in lookup or not isinstance(rule, str) or not rule:
            raise ValueError("A formal pair has an unknown case or missing rule identifier.")
        if rule in grouped[case_id]:
            raise ValueError(f"Duplicate formal pair: {case_id}, {rule}.")
        if row.get("activation_status") not in {"active", "inactive", "not_applicable"}:
            raise ValueError("Unknown formal activation status.")
        if row.get("formal_outcome") not in {"satisfied", "violated", "vacuously_satisfied"}:
            raise ValueError("Unknown formal outcome.")
        kinds[rule].add(row["activation_status"] == "not_applicable")
        grouped[case_id][rule] = dict(row)
    rules = set(kinds)
    if not rules or any(len(kind) != 1 for kind in kinds.values()):
        raise ValueError("Unary and event-triggered rule classifications must be consistent.")
    profiles = []
    for case_id, case in lookup.items():
        rows = grouped[case_id]
        if set(rows) != rules:
            raise ValueError(f"Missing formal case/rule pairs for {case_id}.")
        profile = {**case, "case_year": inputs.case_year(case_id), "formal_results": rows}
        for status, name in (("active", "active"), ("inactive", "inactive"), ("not_applicable", "unary")):
            ids = sorted((rule for rule in rules if rows[rule]["activation_status"] == status), key=_natural)
            profile[f"{name}_rule_ids"] = ids
            profile[f"n_{name}"] = len(ids)
        outcomes = Counter(row["formal_outcome"] for row in rows.values())
        for outcome, name in (("satisfied", "satisfied_non_vacuously"), ("violated", "violated"),
                              ("vacuously_satisfied", "vacuous")):
            profile[f"n_formal_{name}"] = outcomes[outcome]
        profiles.append(profile)
    return sorted(profiles, key=_order)


def select_activation_cohort(profiles: list[dict], quotas: dict[int, int], seed: int,
                             *, required_case_ids=()) -> list[dict]:
    """Exactly maximise annual active-rule totals; break boundary ties heuristically."""
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("The seed must be an integer.")
    if len({row["case_id"] for row in profiles}) != len(profiles):
        raise ValueError("Candidate case identifiers must be unique.")
    if not quotas or any(isinstance(y, bool) or not isinstance(y, int)
                         or isinstance(q, bool) or not isinstance(q, int) or q < 0 for y, q in quotas.items()):
        raise ValueError("Annual quotas must contain integer years and non-negative integer counts.")
    required = list(required_case_ids)
    lookup = {row["case_id"]: row for row in profiles}
    if len(required) != len(set(required)) or any(identifier not in lookup for identifier in required):
        raise ValueError("Required case identifiers must be unique and eligible.")
    selected = [lookup[identifier] for identifier in required]
    if any(_order(row)[0] not in quotas for row in selected):
        raise ValueError("A required case is outside the annual quotas.")
    frequency = Counter(rule for row in profiles for rule in set(row["active_rule_ids"]))
    boundaries, slots = [], {}
    for year, quota in sorted(quotas.items()):
        forced = sum(_order(row)[0] == year for row in selected)
        if forced > quota:
            raise ValueError(f"Required cases exceed the quota for {year}.")
        pool = [row for row in profiles if _order(row)[0] == year and row["case_id"] not in required]
        quota -= forced
        if quota > len(pool):
            raise ValueError(f"The quota for {year} exceeds its eligible pool.")
        if not quota:
            continue
        cutoff = sorted((len(set(row["active_rule_ids"])) for row in pool), reverse=True)[quota - 1]
        strict = [row for row in pool if len(set(row["active_rule_ids"])) > cutoff]
        selected.extend(strict)
        slots[year] = quota - len(strict)
        boundaries.extend(row for row in pool if len(set(row["active_rule_ids"])) == cutoff)
    coverage = Counter(rule for row in selected for rule in set(row["active_rule_ids"]))
    while any(slots.values()):
        available = [row for row in boundaries if slots[_order(row)[0]]]
        def priority(row: dict) -> tuple:
            gain = sum((Fraction(1, frequency[rule] * (coverage[rule] + 1))
                        for rule in set(row["active_rule_ids"])), Fraction())
            tie = hashlib.sha256(f"{seed}\0{row['case_id']}".encode()).hexdigest()
            return (-gain, tie, row["case_id"])
        chosen = min(available, key=priority)
        selected.append(chosen)
        boundaries.remove(chosen)
        slots[_order(chosen)[0]] -= 1
        coverage.update(set(chosen["active_rule_ids"]))
    return sorted(selected, key=_order)


def _public(row: dict, baseline_ids: set[str]) -> dict:
    return {key: value for key, value in row.items() if key not in {"events", "formal_results"}} | {
        "event_activity_counts": dict(Counter(event["activity"] for event in row.get("events", []))),
        "overlap_baseline": row["case_id"] in baseline_ids,
        "txt_file_count": sum(Path(name).suffix.lower() == ".txt" for name in row.get("document_files", [])),
        "html_file_count": sum(Path(name).suffix.lower() == ".html" for name in row.get("document_files", []))}


def _aggregate(label: str, rows: list[dict]) -> dict:
    result = {"cohort": label, "cases": len(rows)}
    for key in ("n_active", "n_inactive", "n_unary", "n_formal_satisfied_non_vacuously", "n_formal_violated", "n_formal_vacuous"):
        result[key] = sum(row[key] for row in rows)
    result["total_rule_pairs"] = result["n_active"] + result["n_inactive"] + result["n_unary"]
    result["event_triggered_rule_pairs"] = result["n_active"] + result["n_inactive"]
    result["non_vacuous_rule_pairs"] = result["n_active"] + result["n_unary"]
    for activity, field in (("Judgment", "cases_with_judgment"), ("Order (OJ)", "cases_with_order_oj"),
                            ("Opinion (AG)", "cases_with_ag_opinion")):
        result[field] = sum(any(event["activity"] == activity for event in row.get("events", [])) for row in rows)
    return result


def _coverage(profiles: list[dict], baseline: list[dict], selected: list[dict], strict: list[dict]) -> list[dict]:
    result = []
    for rule in sorted(profiles[0]["formal_results"], key=_natural):
        row = {"rule_id": rule, "constraint": profiles[0]["formal_results"][rule].get("constraint")}
        for prefix, cohort in (("eligible", profiles), ("baseline", baseline), ("strict_maximum", strict), ("selected", selected)):
            statuses = Counter(case["formal_results"][rule]["activation_status"] for case in cohort)
            outcomes = Counter(case["formal_results"][rule]["formal_outcome"] for case in cohort)
            for status, name in (("active", "active"), ("inactive", "inactive"), ("not_applicable", "unary")):
                row[f"{prefix}_{name}"] = statuses[status]
            for outcome in ("satisfied", "violated", "vacuously_satisfied"):
                row[f"{prefix}_{outcome}"] = outcomes[outcome]
        result.append(row)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--archive", default="data/declare_results_current.zip")
    parser.add_argument("--output-dir")
    parser.add_argument("--baseline-dir", default=DEFAULT_BASELINE)
    parser.add_argument("--min-order-oj-cases", type=int, choices=(0, 2), default=2,
                        help="Preserve two Order (OJ) cases (default), or maximise activation under annual quotas alone.")
    args = parser.parse_args(argv)
    try:
        config, root = p.load_config(args.config)
        config_sha = inputs.sha256_file(args.config)
        input_hashes = {name: inputs.sha256_file(p.resolve(root, config[name]))
                        for name in ("rules_csv", "rules_decl", "event_log_csv")}
        if (config["year_start"], config["year_end"], config["cases_per_year"], config["total_cases"],
                config["min_events"], config["country"], config["case_category"], config["case_ids"]) != (2016, 2026, 5, 50, 5, "EN", "C", None):
            raise ValueError("This selection requires the agreed 2016–2026, 50-case, five-per-year EN/C/min5 configuration.")
        catalogue = p.load_catalogue(p.resolve(root, config["rules_csv"]))
        p.validate_decl_catalogue(p.resolve(root, config["rules_decl"]), catalogue)
        rules, _ = p.select_requirements(catalogue, config["rule_count"], config["rule_ids"],
                                         config["variant_ids"], config["sampling_seed"])
        if len(rules) != 19:
            raise ValueError("The full catalogue of 19 distinct base rules is required.")
        log = p.resolve(root, config["event_log_csv"])
        archive = p.resolve(root, args.archive)
        archive_sha = inputs.sha256_file(archive)
        candidates, _, cleaning = inputs.load_cases(log, min_events=config["min_events"],
            category=config["case_category"], country=config["country"], year_start=config["year_start"],
            year_end=config["year_end"], case_texts_dir=p.resolve(root, config["case_texts_dir"]))
        _, annual = p.select_cases_by_year(candidates, 2016, 2026, 5, config["sampling_seed"], total_cases=50)
        quotas = {row["year"]: row["sampled_cases"] for row in annual}
        formal_rows, provenance = formal.import_formal_results(archive, candidates, rules, log, 5)
        profiles = build_profiles(candidates, formal_rows)
        seed = config["sampling_seed"]
        strict = select_activation_cohort(profiles, quotas, seed)
        selected = strict
        boundary_alternatives = 0
        if args.min_order_oj_cases:
            order_cases = [row for row in profiles if any(event["activity"] == "Order (OJ)" for event in row["events"])]
            if len(order_cases) < args.min_order_oj_cases:
                raise ValueError("Insufficient eligible Order (OJ) cases for the coverage constraint.")
            frequency = Counter(rule for row in profiles for rule in row["active_rule_ids"])
            alternatives = []
            for pair in combinations(order_cases, args.min_order_oj_cases):
                ids = tuple(sorted(row["case_id"] for row in pair))
                try:
                    cohort = select_activation_cohort(profiles, quotas, seed, required_case_ids=ids)
                except ValueError:
                    continue  # Forced pair cannot fit the fixed annual quotas.
                counts = Counter(rule for row in cohort for rule in row["active_rule_ids"])
                objective = sum((sum((Fraction(1, frequency[rule] * i) for i in range(1, count + 1)), Fraction())
                                 for rule, count in counts.items()), Fraction())
                tie = hashlib.sha256(f"{seed}\0{'|'.join(ids)}".encode()).hexdigest()
                alternatives.append((-sum(row["n_active"] for row in cohort), -objective, tie, cohort))
            if not alternatives:
                raise ValueError("No Order (OJ) coverage constraint fits the annual quotas.")
            boundary_alternatives = len(alternatives)
            selected = min(alternatives, key=lambda item: item[:3])[3]
        baseline_dir = p.resolve(root, args.baseline_dir)
        audit = baseline_dir / "_audit"
        manifest_path, pairs_path = audit / "run_manifest.json", audit / "prepared/pairs.jsonl"
        baseline_manifest = p.read_json(manifest_path)
        if baseline_manifest["code_sha256"] != BASELINE_MODEL_CODE_SHA256:
            raise ValueError("Baseline source identity differs from the preserved model protocol.")
        if baseline_manifest["prepared_sha256"].get("prepared/pairs.jsonl") != inputs.sha256_file(pairs_path):
            raise ValueError("The baseline pair file differs from its frozen manifest.")
        baseline_pairs = p.read_jsonl(pairs_path)
        baseline_ids = {row["case_id"] for row in baseline_pairs}
        by_id = {row["case_id"]: row for row in profiles}
        if not baseline_ids.issubset(by_id) or len(baseline_ids) != 50:
            raise ValueError("The baseline must contain 50 currently eligible cases.")
        expected = {(identifier, rule["base_rule_id"]) for identifier in baseline_ids for rule in rules}
        if len(baseline_pairs) != len(expected) or {(row["case_id"], row["base_rule_id"]) for row in baseline_pairs} != expected:
            raise ValueError("The baseline case/rule matrix is not complete and unique.")
        if any(row["trace_sha256"] != by_id[row["case_id"]]["trace_sha256"] for row in baseline_pairs):
            raise ValueError("A baseline trace differs from the eligible input trace.")
        baseline = sorted((by_id[identifier] for identifier in baseline_ids), key=_order)
        if Counter(row["case_year"] for row in baseline) != Counter({y: q for y, q in quotas.items() if q}):
            raise ValueError("The baseline does not use the same annual quotas.")
        members = {}
        with formal._ReferenceFiles(archive) as reference:
            for name in ("run_manifest.json", "cleaning_manifest.json", provenance["clean_event_log_name"],
                         "rule_catalogue.csv", "case_rule_results.csv"):
                member = reference._resolve(name)
                members[str(member)] = hashlib.sha256(reference.read(name)).hexdigest()
        if len({str(Path(member).parent) for member in members}) != 1:
            raise ValueError("Resolved current reference members must share one results directory.")
        if (archive_sha != inputs.sha256_file(archive) or config_sha != inputs.sha256_file(args.config)
                or any(value != inputs.sha256_file(p.resolve(root, config[name])) for name, value in input_hashes.items())):
            raise ValueError("A selection input changed during validation; no output was created.")
        comparison = [_aggregate("baseline", baseline), _aggregate("strict_maximum", strict),
                      _aggregate("recommended" if args.min_order_oj_cases else "selected_annual_only", selected)]
        now = datetime.now(ZoneInfo("Europe/Rome"))
        output = p.resolve(root, args.output_dir or f"outputs/activation_selection_2016_2026_50_cases_{now:%Y%m%d_%H%M%S}")
        output.mkdir(exist_ok=False)
        (output / "_audit").mkdir()
        p.write_table(output / "selected_cases.csv", [_public(row, baseline_ids) for row in selected])
        p.write_table(output / "strict_maximum_cases.csv", [_public(row, baseline_ids) for row in strict])
        p.write_table(output / "rule_coverage.csv", _coverage(profiles, baseline, selected, strict))
        p.write_table(output / "comparison.csv", comparison)
        p.write_table(output / "_audit/candidates.csv", [_public(row, baseline_ids) for row in profiles])
        p.write_json(output / "selected_case_ids.json", [row["case_id"] for row in selected])
        report = ["CURIA activation-based case selection (offline)", f"Created: {now.isoformat()}",
            f"Eligible cases: {len(profiles)}; selected: {len(selected)}; seed: {config['sampling_seed']}",
            "Annual quotas (CaseID filing year): " + "; ".join(f"{y}: {q}" for y, q in quotas.items()),
            f"Active case/rule pairs: {comparison[2]['n_active']} selected; {comparison[1]['n_active']} annual-only maximum; {comparison[0]['n_active']} baseline.",
            f"Inactive case/rule pairs: {comparison[2]['n_inactive']} selected; {comparison[1]['n_inactive']} annual-only maximum; {comparison[0]['n_inactive']} baseline.",
            "The annual-only maximum retains the same year quotas and omits only the Order (OJ) coverage constraint.",
            f"Non-vacuous pairs (active plus unary): {comparison[2]['non_vacuous_rule_pairs']}/{comparison[2]['total_rule_pairs']} selected.",
            f"Coverage constraint: at least {args.min_order_oj_cases} cases containing the recorded activity Order (OJ); {boundary_alternatives} feasible forced-pair alternatives evaluated.",
            "Primary objective: exact maximum sum of distinct active base rules, subject to the annual quotas and stated event-coverage constraint.",
            "Secondary boundary-only coverage selection is a deterministic greedy heuristic, not a global coverage optimum.",
            "Unary constraints are counted separately and remain evaluated for every case.",
            "The formal satisfied-non-vacuously counter excludes vacuous satisfaction; the three formal outcome counters partition all case/rule pairs.",
            "Formal activation does not establish legal applicability or legal correctness. Formal violations are retained; outcomes do not enter selection.",
            "This purposive cohort favours recorded trigger coverage and is not a representative random sample. No LLM calls were made.",
            "Event coverage (baseline → annual-only maximum → selected): " + "; ".join(f"{field}: {comparison[0][field]} → {comparison[1][field]} → {comparison[2][field]}"
                for field in ("cases_with_judgment", "cases_with_order_oj", "cases_with_ag_opinion")),
            f"Selected cases without a recorded Judgment activity: {len(selected) - comparison[2]['cases_with_judgment']}.",
            "Event types can be lost when maximising activation; review rule_coverage.csv and this comparison before adopting this cohort.",
            "", "Selected CaseID values", *[f"{i}. {row['case_id']}" for i, row in enumerate(selected, 1)]]
        (output / "selection_report.txt").write_text("\n".join(report) + "\n", encoding="utf-8")
        selection_manifest = {"created": now.isoformat(), "configuration_file": str(Path(args.config).resolve()),
            "configuration_sha256": config_sha, "normalised_configuration": config,
            "selection_parameters": {"year_start": 2016, "year_end": 2026, "cases_per_year": 5, "total_cases": 50,
                "seed": config["sampling_seed"], "quotas": quotas, "min_events": 5, "country": "EN", "category": "C",
                "min_order_oj_cases": args.min_order_oj_cases, "feasible_forced_pairs": boundary_alternatives},
            "algorithm": "Enumerate feasible forced Order(OJ) pairs if constrained; annual exact top-k active distinct base rules; >cutoff preselection; cutoff-only greedy Fraction(1/(eligible_frequency*(coverage+1))); pair ranking by exact active total then weighted harmonic coverage then SHA256(seed NUL pairIDs); SHA256(seed NUL CaseID) boundary ties",
            "primary_objective_is_exact": True, "secondary_coverage_is_heuristic": True,
            "input_sha256": input_hashes, "archive": str(archive), "archive_sha256": archive_sha,
            "resolved_archive_members_sha256": members, "formal_provenance": provenance, "cleaning": cleaning,
            "document_presence_sha256": p.digest({row["case_id"]: row["document_files"] for row in profiles}),
            "baseline_directory": str(baseline_dir), "baseline_manifest_sha256": inputs.sha256_file(manifest_path),
            "baseline_pairs_sha256": inputs.sha256_file(pairs_path), "source_code_sha256": inputs.sha256_file(__file__),
            "baseline_model_code_sha256": baseline_manifest["code_sha256"],
            "selection_runtime_code_sha256": p.code_digest(), "selected_case_ids_sha256": inputs.sha256_file(output / "selected_case_ids.json"),
            "comparison": comparison, "llm_calls": 0,
            "output_sha256": {str(path.relative_to(output)): inputs.sha256_file(path)
                              for path in sorted(output.rglob("*")) if path.is_file()}}
        p.write_json(output / "_audit/selection_manifest.json", selection_manifest)
        print(f"Offline selection saved to {output}")
        return 0
    except (ValueError, OSError, KeyError) as exc:
        parser.exit(2, f"Selection stopped: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
