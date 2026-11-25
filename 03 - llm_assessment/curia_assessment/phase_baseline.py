"""Reuse frozen phase-1 evidence, without sampling or exposing its answers."""

from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

from . import pipeline as p
from .case_prompts import build_case_prompts
from .case_recovery import read_case_attempt
from .inputs import requirement_only


def _frozen_identity(config: dict, root: Path, field: str, label: str) -> dict | None:
    if config.get(field) is None:
        return None
    audit = p.resolve(root, config[field]) / "_audit"
    manifest_path = audit / "run_manifest.json"
    manifest = p.read_json(manifest_path)
    identity = {key: manifest[key] for key in
                ("configuration", "input_sha256", "code_sha256", "pipeline_version")}
    if p.digest(identity) != manifest["experiment_sha256"]:
        raise ValueError(f"The {label} manifest identity is inconsistent.")
    for relative, expected in manifest["prepared_sha256"].items():
        path = (audit / relative).resolve()
        if not path.is_relative_to(audit.resolve()) or not path.is_file() or p.sha256_file(path) != expected:
            raise ValueError(f"The frozen {label} evidence changed: {relative}.")
    return {"manifest_sha256": p.sha256_file(manifest_path),
            "experiment_sha256": manifest["experiment_sha256"],
            "prepared_sha256": manifest["prepared_sha256"]}


def baseline_identity(config: dict, root: Path) -> dict | None:
    return _frozen_identity(config, root, "baseline_phase1_dir", "phase-1")


def cohort_identity(config: dict, root: Path) -> dict | None:
    return _frozen_identity(config, root, "cohort_source_dir", "cohort")


def _table(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def _csv_value(value) -> str:
    if value is None:
        return ""
    return p.canonical(value) if isinstance(value, (dict, list)) else str(value)


def load_frozen_cohort(config: dict, root: Path) -> dict:
    """Reuse only frozen sampling/evidence inputs for an intentional new protocol.

    Historical responses, request hashes and prompts are never imported. The
    new experiment must prepare and issue fresh requests under its own source
    identity. This differs from B's strict same-protocol phase-1 baseline.
    """
    identity = cohort_identity(config, root)
    if identity is None:
        raise ValueError("No frozen cohort is configured.")
    audit = p.resolve(root, config["cohort_source_dir"]) / "_audit"
    manifest = p.read_json(audit / "run_manifest.json")
    previous = manifest["configuration"]
    if previous["conditions"] != ["A"] or previous["assessment_mode"] != "case":
        raise ValueError("The frozen cohort source must be a log-only case experiment.")
    if p.resolve(root, config["output_dir"]) == audit.parent:
        raise ValueError("A fresh cohort run requires a separate output directory.")
    for name in ("min_events", "case_category", "country", "year_start", "year_end", "cases_per_year",
                 "total_cases", "case_ids", "variant_ids", "rule_ids", "rule_count", "sampling_seed", "formal_mode"):
        if config[name] != previous[name]:
            raise ValueError(f"Frozen cohort selection must retain its setting: {name}.")
    for name in ("rules_csv", "rules_decl", "event_log_csv", "formal_results", "evidence_jsonl"):
        if config[name] is not None and p.path_digest(p.resolve(root, config[name])) != manifest["input_sha256"][name]:
            raise ValueError(f"A frozen cohort input changed: {name}.")
    presence = p.digest(sorted(p.index_case_documents(p.resolve(root, config["case_texts_dir"]))))
    if presence != manifest["input_sha256"]["case_texts_dir"]:
        raise ValueError("The frozen cohort document-presence index changed.")
    pairs = [requirement_only(row) for row in p.read_jsonl(audit / "prepared/pairs.jsonl")]
    by_case = defaultdict(list)
    for pair in pairs:
        if pair.get("passages"):
            raise ValueError("The frozen cohort source contains document evidence.")
        by_case[pair["case_id"]].append(pair)
    selected = _table(audit / "prepared/selected_cases.csv")
    case_ids = [row["case_id"] for row in selected]
    if (len(case_ids) != len(set(case_ids)) or case_ids != list(by_case)
            or len(pairs) != len({pair["pair_id"] for pair in pairs})
            or len(case_ids) != manifest["sampled_cases"] or len(pairs) != manifest["selected_pairs"]):
        raise ValueError("The frozen cohort case/pair allocation is inconsistent.")
    rules = [requirement_only(row) for row in _table(audit / "prepared/selected_rules.csv")]
    expected_rules = [row["base_rule_id"] for row in rules]
    if len(expected_rules) != len(set(expected_rules)) or any(
        [row["base_rule_id"] for row in members] != expected_rules for members in by_case.values()
    ):
        raise ValueError("The frozen cohort must share one ordered rule formulation set across every case.")
    for members in by_case.values():
        for pair, rule in zip(members, rules, strict=True):
            if any(key not in pair or _csv_value(pair[key]) != value for key, value in rule.items()):
                raise ValueError("The frozen cohort rule formulations disagree with the selected-rule register.")
    cases = []
    for record in selected:
        pair = by_case[record["case_id"]][0]
        if any(key not in pair or _csv_value(pair[key]) != value for key, value in record.items()):
            raise ValueError("The frozen cohort case metadata disagree with the selected-case register.")
        case = {key: pair[key] for key in record}
        case["events"] = pair["events"]
        if any(any(member[key] != value for key, value in case.items()) for member in by_case[record["case_id"]]):
            raise ValueError("The frozen cohort contains inconsistent shared case evidence.")
        cases.append(case)
    return {"cases": cases, "rules": rules, "pairs": pairs,
            "annual": manifest["case_sampling_by_year"],
            "cleaning": p.read_json(audit / "prepared/cleaning/selection_summary.json"),
            "formal": p.read_jsonl(audit / "prepared/formal_results.jsonl"),
            "formal_provenance": p.read_json(audit / "prepared/formal_provenance.json"),
            "rule_audit": [requirement_only(row) for row in _table(audit / "prepared/rule_selection.csv")], "manifest": manifest,
            "provenance": {**identity, "method": "Reuse frozen cohort inputs; prepare fresh requests for the new protocol.",
                           "source_directory": config["cohort_source_dir"], "model_responses_reused": 0}}


def load_phase1_baseline(config: dict, root: Path) -> dict:
    """Require an unchanged protocol and reproduce every archived A request.

    The original executable hash is preserved as historical provenance rather
    than compared with the new operational code. All frozen input bytes and
    phase-1 request hashes are checked. No response text is loaded as evidence.
    """
    identity = baseline_identity(config, root)
    if identity is None:
        raise ValueError("No phase-1 baseline is configured.")
    audit = p.resolve(root, config["baseline_phase1_dir"]) / "_audit"
    manifest = p.read_json(audit / "run_manifest.json")
    previous = manifest["configuration"]
    if previous["conditions"] != ["A"] or previous["assessment_mode"] != "case":
        raise ValueError("The baseline must contain only case assessments in phase 1.")
    previous_protocol = previous.get("case_protocol", "requirement_declare_v1")
    if config.get("case_protocol", "requirement_declare_v1") != previous_protocol:
        raise ValueError("Phase 2 must retain the phase-1 setting: case_protocol.")
    exceptions = {"conditions", "output_dir", "max_document_characters", "max_case_characters",
                  "baseline_phase1_dir", "ocr_sidecars_dir", "deduplicate_exact_text", "exclude_identifier_only"}
    for name, value in previous.items():
        if name not in exceptions and config.get(name) != value:
            raise ValueError(f"Phase 2 must retain the phase-1 setting: {name}.")
    if p.resolve(root, config["output_dir"]) == audit.parent:
        raise ValueError("Phase 2 requires a separate output directory.")
    for name in ("rules_csv", "rules_decl", "event_log_csv", "formal_results", "evidence_jsonl"):
        if config[name] is not None and p.path_digest(p.resolve(root, config[name])) != manifest["input_sha256"][name]:
            raise ValueError(f"A phase-1 input changed: {name}.")
    pairs = [requirement_only(row) for row in p.read_jsonl(audit / "prepared/pairs.jsonl")]
    by_case = defaultdict(list)
    for pair in pairs:
        if pair.get("passages"):
            raise ValueError("The baseline contains document evidence.")
        by_case[pair["case_id"]].append(pair)
    jobs = p.read_jsonl(audit / "prepared/planned_assessments.jsonl")
    providers = {provider["id"]: provider for provider in config["providers"]}
    checks = []
    for job in jobs:
        members = by_case[job["case_id"]]
        if job["condition"] != "A" or [pair["pair_id"] for pair in members] != job["pair_ids"]:
            raise ValueError("The phase-1 case allocation is inconsistent.")
        if job.get("case_protocol", "requirement_declare_v1") != previous_protocol:
            raise ValueError("The phase-1 job protocol differs from its frozen configuration.")
        system, user, _ = build_case_prompts(members, "A", case_protocol=previous_protocol)
        request = p.build_request(providers[job["provider_id"]], system, user, config["max_output_tokens"])
        if p.digest([system, user]) != job["prompt_sha256"] or p.digest(request) != job["request_sha256"]:
            raise ValueError(f"Phase-1 request cannot be reproduced exactly: {job['job_id']}. "
                             "Both phases require the same requirement-and-DECLARE protocol; prepare a new phase 1 "
                             "before pairing phase 2 with a study prepared under a different prompt protocol.")
        attempts = p._attempt_directories(audit, job)
        if not attempts or not (attempts[-1] / "assessment.json").is_file():
            raise ValueError("Every baseline case assessment must have a completed result.")
        # Only the completion flag is consulted; no model answer is supplied to B.
        if read_case_attempt(audit, job, attempts[-1])["status"] != "ok":
            raise ValueError("The phase-1 baseline contains an incomplete assessment.")
        archived = p.read_json(attempts[-1] / "request.json")
        if p.digest(archived) != job["request_sha256"]:
            raise ValueError("An archived phase-1 request differs from the frozen protocol.")
        checks.append({"job_id": job["job_id"], "case_id": job["case_id"],
                       "provider_id": job["provider_id"], "prompt_sha256": job["prompt_sha256"],
                       "request_sha256": job["request_sha256"]})
    selected = _table(audit / "prepared/selected_cases.csv")
    case_ids = [row["case_id"] for row in selected]
    expected_allocation = {(case_id, provider, run) for case_id in case_ids for provider in providers
                           for run in range(1, config["repetitions"] + 1)}
    actual_allocation = [(job["case_id"], job["provider_id"], job["run"]) for job in jobs]
    if (len(case_ids) != len(set(case_ids)) or set(case_ids) != set(by_case)
            or len({job["job_id"] for job in jobs}) != len(jobs)
            or len(actual_allocation) != len(expected_allocation) or set(actual_allocation) != expected_allocation):
        raise ValueError("The phase-1 baseline does not cover each case/model/run exactly once.")
    cases = []
    for record in selected:
        pair = by_case[record["case_id"]][0]
        case = {key: pair[key] for key in record}
        case["events"] = pair["events"]
        cases.append(case)
    if len(cases) != manifest["sampled_cases"] or len(jobs) != len(cases) * len(providers) * config["repetitions"]:
        raise ValueError("The baseline does not have a complete crossed case/model design.")
    rules = [requirement_only(row) for row in _table(audit / "prepared/selected_rules.csv")]
    return {"cases": cases, "rules": rules, "pairs": pairs,
            "annual": manifest["case_sampling_by_year"],
            "cleaning": p.read_json(audit / "prepared/cleaning/selection_summary.json"),
            "formal": p.read_jsonl(audit / "prepared/formal_results.jsonl"),
            "formal_provenance": p.read_json(audit / "prepared/formal_provenance.json"),
            "rule_audit": [requirement_only(row) for row in _table(audit / "prepared/rule_selection.csv")],
            "manifest": manifest,
            "provenance": {**identity, "method": "Reuse frozen phase-1 pairs; no resampling or reformulation.",
                           "phase_1_requests_reproduced": checks}}
