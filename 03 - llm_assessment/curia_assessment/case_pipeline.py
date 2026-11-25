"""Assess all supplied rules per case, preserving private audit and expert outputs."""

from __future__ import annotations

import threading
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from . import pipeline as p
from .case_prompts import build_case_prompts, parse_case_assessment
from .comparison import compare_rows, summarise_comparisons, COMPARISON_CATEGORIES
from .prompts import OUTCOMES
from .evaluation_exports import write_evaluation_outputs
from .metrics import normalise_usage, summarise_resource_usage
from .reporting import write_case_extraction_report, write_results_report
from .text_evidence import build_document_evidence
from .case_recovery import read_case_attempt
from .article_reporting import write_article_outputs
from .resource_reporting import write_model_resources
from .event_log_statistics import write_event_log_statistics
from .resumption import classify_provider_stop
from .run_checkpoint import write_run_checkpoint
from .case_archive import recover_case_assessment
from .phase_baseline import baseline_identity, load_phase1_baseline, cohort_identity, load_frozen_cohort


def _locations(config: dict, root: Path) -> tuple[Path, Path]:
    output = p.resolve(root, config["output_dir"])
    return output, output / "_audit"


def _document_sources(config: dict, root: Path, documents: list[dict], *, expected: bool = False) -> dict:
    base = p.resolve(root, config["case_texts_dir"])
    sources = {}
    for row in documents:
        name = row.get("source_file")
        if not name or not row.get("source_sha256"):
            continue
        path = (base / name).resolve()
        if not path.is_relative_to(base):
            raise ValueError("A documentary source must remain within case_texts_dir.")
        sources[name] = row["source_sha256"] if expected else p.sha256_file(path)
        if row.get("ocr_source_file"):
            if not config.get("ocr_sidecars_dir"):
                raise ValueError("An OCR source requires ocr_sidecars_dir.")
            ocr_base = p.resolve(root, config["ocr_sidecars_dir"])
            ocr_path = (ocr_base / row["ocr_source_file"]).resolve()
            if not ocr_path.is_relative_to(ocr_base):
                raise ValueError("An OCR sidecar must remain within ocr_sidecars_dir.")
            sources["ocr:" + row["ocr_source_file"]] = (row["ocr_source_sha256"] if expected
                                                         else p.sha256_file(ocr_path) if ocr_path.is_file() else None)
    return sources


def _identity(config: dict, root: Path, documents: list[dict]) -> dict:
    identity = p.experiment_identity(config, root)
    sources = _document_sources(config, root, documents)
    identity["input_sha256"]["phase_2_documents"] = p.digest(sources)
    if config.get("baseline_phase1_dir"):
        identity["input_sha256"]["phase_1_baseline"] = p.digest(baseline_identity(config, root))
    if config.get("cohort_source_dir"):
        identity["input_sha256"]["frozen_cohort"] = p.digest(cohort_identity(config, root))
    payload = {key: value for key, value in identity.items() if key != "experiment_sha256"}
    return {**payload, "experiment_sha256": p.digest(payload)}


def _verify(config: dict, root: Path, audit: Path) -> dict:
    documents = p.read_jsonl(audit / "prepared/document_selection.jsonl")
    return p.verify_prepared(audit, _identity(config, root, documents))


def prepare_case(config_path: str | Path) -> dict:
    config, root = p.load_config(config_path)
    output, audit = _locations(config, root)
    with p.output_lock(audit):
        if (audit / "run_manifest.json").exists():
            return _verify(config, root, audit)
        if (audit / "requests").exists():
            raise ValueError("Unmanifested requests exist; use a new output_dir.")
        prepared = audit / "prepared"
        variants = p.load_catalogue(p.resolve(root, config["rules_csv"]))
        if config["rules_decl"] is not None:
            p.validate_decl_catalogue(p.resolve(root, config["rules_decl"]), variants)
        is_paired_baseline = bool(config.get("baseline_phase1_dir"))
        baseline = (load_phase1_baseline(config, root) if is_paired_baseline else
                    load_frozen_cohort(config, root) if config.get("cohort_source_dir") else None)
        if baseline:
            cases, rules, rule_audit = baseline["cases"], baseline["rules"], baseline["rule_audit"]
            annual, cleaning = baseline["annual"], baseline["cleaning"]
            eligible_count = baseline["manifest"]["eligible_cases"]
            p.write_json(prepared / ("baseline_provenance.json" if is_paired_baseline else "cohort_provenance.json"),
                         baseline["provenance"])
        else:
            rules, rule_audit = p.select_requirements(variants, config["rule_count"], config["rule_ids"],
                                                     config["variant_ids"], config["sampling_seed"])
            eligible, _, cleaning = p.load_cases(
                p.resolve(root, config["event_log_csv"]), min_events=config["min_events"],
                category=config["case_category"], country=config["country"],
                year_start=config["year_start"], year_end=config["year_end"],
                case_texts_dir=p.resolve(root, config["case_texts_dir"]), output_dir=prepared / "cleaning")
            cases, annual = p.select_cases_by_year(eligible, config["year_start"], config["year_end"],
                                                 config["cases_per_year"], config["sampling_seed"], config["case_ids"],
                                                 total_cases=config["total_cases"])
            eligible_count = len(eligible)
        passages, document_audit = ({}, [])
        if "B" in config["conditions"]:
            passages, document_audit = build_document_evidence(
                cases, p.resolve(root, config["case_texts_dir"]),
                max_document_characters=config["max_document_characters"],
                max_case_characters=config["max_case_characters"],
                deduplicate_exact_text=config["deduplicate_exact_text"],
                exclude_identifier_only=config["exclude_identifier_only"],
                ocr_sidecars_dir=(p.resolve(root, config["ocr_sidecars_dir"])
                                  if config["ocr_sidecars_dir"] else None))
        pairs = baseline["pairs"] if baseline else p.cross_pairs(cases, rules)
        for pair in pairs:
            pair["passages"] = passages.get(pair["case_id"], [])
        if baseline:
            formal, provenance = baseline["formal"], baseline["formal_provenance"]
        elif config["formal_mode"] == "import":
            formal, provenance = p.import_formal_results(p.resolve(root, config["formal_results"]), cases, rules,
                                                         p.resolve(root, config["event_log_csv"]), config["min_events"])
        else:
            formal = p.compute_formal_results(cases, rules, prepared / "computed_formal")
            provenance = {"mode": "compute"}
        by_case = defaultdict(list)
        for pair in pairs:
            by_case[pair["case_id"]].append(pair)
        jobs = []
        for condition in config["conditions"]:
            for repetition in range(1, config["repetitions"] + 1):
                for case in cases:
                    members = by_case[case["case_id"]]
                    system, user, _ = build_case_prompts(members, condition,
                                                         passages.get(case["case_id"]) if condition == "B" else None,
                                                         case_protocol=config["case_protocol"])
                    for provider in config["providers"]:
                        request = p.build_request(provider, system, user, config["max_output_tokens"])
                        job = {"provider_id": provider["id"], "provider": provider["provider"], "model": provider["model"],
                               "condition": condition, "run": repetition, "case_id": case["case_id"],
                               "case_year": case["case_year"], "pair_ids": [pair["pair_id"] for pair in members],
                               "rule_ids": [pair["base_rule_id"] for pair in members],
                               "case_protocol": config["case_protocol"],
                               "request_sha256": p.digest(request), "prompt_sha256": p.digest([system, user])}
                        job["job_id"] = "case_" + p.digest(job)[:24]
                        jobs.append(job)
        p.write_jsonl(prepared / "pairs.jsonl", pairs)
        p.write_jsonl(prepared / "planned_assessments.jsonl", jobs)
        p.write_jsonl(prepared / "formal_results.jsonl", formal)
        p.write_jsonl(prepared / "document_selection.jsonl", document_audit)
        p.write_json(audit / "recovery_plan.json", {"max_output_tokens": config["recovery_max_output_tokens"], "entries": []})
        p.write_json(prepared / "formal_provenance.json", provenance)
        p.write_json(prepared / "cleaning/selection_summary.json", cleaning)
        p.write_table(prepared / "selected_cases.csv", [{k: v for k, v in case.items() if k != "events"} for case in cases])
        p.write_table(prepared / "selected_rules.csv", rules)
        p.write_table(prepared / "rule_selection.csv", rule_audit)
        p.write_table(prepared / "case_sampling_by_year.csv", annual)
        p.write_table(prepared / "document_selection.csv", document_audit)
        report = write_case_extraction_report(output, config, cases, annual, cleaning)
        # The public extraction report is also protected by the frozen plan.
        identity = _identity(config, root, document_audit)
        expected_sources = _document_sources(config, root, document_audit, expected=True)
        if identity["input_sha256"]["phase_2_documents"] != p.digest(expected_sources):
            raise ValueError("A document changed while phase B evidence was being frozen; prepare again before running models.")
        manifest = {**identity, "created_utc": p.utc_now(), "eligible_cases": eligible_count,
                    "sampled_cases": len(cases), "catalogue_base_rules": len({v["base_rule_id"] for v in variants}),
                    "catalogue_textual_variants": len(variants), "selected_pairs": len(pairs),
                    "selected_base_rules": len(rules), "selected_textual_variants": len(rules),
                    "planned_assessments": len(jobs), "planned_rule_assessments": len(jobs) * len(rules),
                    "protocol_outcomes": sorted(OUTCOMES), "comparison_categories": list(COMPARISON_CATEGORIES),
                    "assessment_unit": "One independent case request containing every selected rule.",
                    "case_sampling_by_year": annual, "case_extraction_report": report.name,
                    "prepared_sha256": {str(path.relative_to(audit)): p.sha256_file(path)
                                        for path in sorted(prepared.rglob("*")) if path.is_file()}}
        # Keep the relative filename inside the audit boundary for verification.
        frozen_report = prepared / report.name
        frozen_report.write_bytes(report.read_bytes())
        manifest["prepared_sha256"][str(frozen_report.relative_to(audit))] = p.sha256_file(frozen_report)
        manifest["prepared_sha256"]["recovery_plan.json"] = p.sha256_file(audit / "recovery_plan.json")
        p.write_json(audit / "run_manifest.json", manifest)
        _analyse(output, audit, config)
    return manifest


def _analyse(output: Path, audit: Path, config: dict) -> dict:
    jobs = p.read_jsonl(audit / "prepared/planned_assessments.jsonl")
    pairs = {row["pair_id"]: row for row in p.read_jsonl(audit / "prepared/pairs.jsonl")}
    log_statistics = write_event_log_statistics(output, pairs.values())
    virtual_jobs, predictions, ledger = [], [], []
    attempts_by_job = {}
    for job in jobs:
        directories = p._attempt_directories(audit, job)
        results = [read_case_attempt(audit, job, directory) for directory in directories]
        attempts_by_job[job["job_id"]] = results
        for number, (directory, result) in enumerate(zip(directories, results, strict=True), 1):
            ledger.append({**job, **result, "attempt": int(directory.name[8:]),
                           "is_latest_attempt": number == len(results)})
        latest = results[-1] if results else {"status": "pending", "assessments": []}
        assessed = {row["rule_id"]: row for row in latest.get("assessments", [])}
        for pair_id in job["pair_ids"]:
            pair = pairs[pair_id]
            virtual = {**{k: job[k] for k in ("provider_id", "provider", "model", "condition", "run", "case_id", "case_year")},
                       "job_id": job["job_id"] + "__" + pair["variant_id"], "pair_id": pair_id,
                       "base_rule_id": pair["base_rule_id"], "variant_id": pair["variant_id"],
                       "requirement_review_id": pair.get("requirement_review_id", ""), "case_request_id": job["job_id"]}
            row = assessed.get(pair["base_rule_id"], {"status": latest["status"], "outcome": None,
                                                     "justification": None, "evidence_refs": [], "error": latest.get("error")})
            virtual_jobs.append(virtual)
            predictions.append({**virtual, **row, "requirement": pair["requirement"], "declare": pair["declare"],
                                "attempt_count": len(results), "selected_attempt": len(results) or None})
    formal = p.read_jsonl(audit / "prepared/formal_results.jsonl")
    comparisons = compare_rows(predictions, formal)
    summaries = summarise_comparisons(comparisons)
    usage = summarise_resource_usage(jobs, ledger)
    lookup = {(row["provider"], row["model"], row["condition"], row["run"]): row for row in usage}
    for row in summaries["model_summary"]:
        row.update(lookup[(row["provider"], row["model"], row["condition"], row["run"])])
    summaries["usage_summary"] = usage
    write_model_resources(output, jobs=jobs, ledger=ledger, usage_summary=usage)
    for key, filename in p.SUMMARY_FILES.items():
        p.write_table((output if key == "model_summary" else audit) / filename, summaries[key])
    p.write_table(audit / "attempts.csv", ledger, None if ledger else ["job_id", "status"])
    p.write_table(audit / "comparison_by_pair.csv", comparisons)
    p.write_json(audit / "analysis_manifest.json", {"created_utc": p.utc_now(), "planned_case_requests": len(jobs),
                 "recorded_api_attempts": len(ledger), "planned_rule_assessments": len(predictions),
                 "output_status_counts": dict(Counter(row["status"] for row in predictions))})
    write_evaluation_outputs(output, config=config, pairs=pairs, jobs=virtual_jobs, assessments=predictions)
    if config["conditions"] in (["A"], ["B"]):
        write_article_outputs(output, config=config, pairs=pairs, jobs=jobs,
                              attempts_by_job=attempts_by_job, formal_rows=formal,
                              event_log_statistics=log_statistics)
    write_results_report(output, summaries)
    return summaries


def analyse_case(config_path: str | Path) -> dict:
    config, root = p.load_config(config_path)
    output, audit = _locations(config, root)
    with p.output_lock(audit):
        _verify(config, root, audit)
        return _analyse(output, audit, config)


def run_case(config_path: str | Path, provider_id: str | None = None,
             max_requests: int | None = None, retry_failed: bool = False) -> dict:
    config, root = p.load_config(config_path)
    providers = {row["id"]: row for row in config["providers"]}
    if provider_id is not None and provider_id not in providers:
        raise ValueError(f"Provider ID is not configured: {provider_id}.")
    if max_requests is not None and (isinstance(max_requests, bool) or not isinstance(max_requests, int) or max_requests < 1):
        raise ValueError("max_requests must be a positive integer.")
    output, audit = _locations(config, root)
    if not (audit / "run_manifest.json").exists():
        prepare_case(config_path)
    counts = {"processed": 0, "successful": 0, "failed": 0, "skipped_existing": 0,
              "blocked_providers": 0, "recovered_from_archive": 0}
    counter_lock = threading.Lock()
    blocked = set()
    provider_stops = {}
    stop = threading.Event()
    reserved = 0
    with p.output_lock(audit):
        _verify(config, root, audit)
        jobs = p.read_jsonl(audit / "prepared/planned_assessments.jsonl")
        pairs = {row["pair_id"]: row for row in p.read_jsonl(audit / "prepared/pairs.jsonl")}
        states = {}
        # Recover a received response before deciding whether a paid retry is
        # necessary. Never repair model text or invent a missing remote reply.
        for job in jobs:
            directories = p._attempt_directories(audit, job)
            if directories:
                for directory in directories:
                    recovered = recover_case_assessment(audit, job, directory, [pairs[key] for key in job["pair_ids"]])
                    counts["recovered_from_archive"] += recovered is not None
                states[job["job_id"]] = read_case_attempt(audit, job, directories[-1])["status"]
            else:
                states[job["job_id"]] = "pending"
            if directories and states[job["job_id"]] != "ok":
                response_path = directories[-1] / "response.json"
                if response_path.is_file():
                    previous_stop = classify_provider_stop(job["provider"], p.read_json(response_path))
                    if previous_stop is not None and (not retry_failed or not previous_stop["can_resume"]):
                        blocked.add(job["provider_id"])
                        provider_stops[job["provider_id"]] = previous_stop
                        if retry_failed and not previous_stop["can_resume"]:
                            print(f"{job['provider_id']}: invalid frozen request; correct the preparation in a "
                                  "new experiment before retrying this provider.", flush=True)
        write_run_checkpoint(audit, jobs, states, provider_stops)

        def process(provider: str, phase: str, stream: int) -> None:
            nonlocal reserved
            matching = [job for job in jobs if job["provider_id"] == provider and job["condition"] == phase]
            for position, job in enumerate(matching):
                if position % config["requests_per_provider"] != stream:
                    continue
                directories = p._attempt_directories(audit, job)
                if directories and (not retry_failed or read_case_attempt(audit, job, directories[-1])["status"] == "ok"):
                    with counter_lock:
                        counts["skipped_existing"] += 1
                    continue
                with counter_lock:
                    if provider in blocked or stop.is_set():
                        return
                    if max_requests is not None and reserved >= max_requests:
                        return
                    reserved += 1
                    states[job["job_id"]] = "running"
                    write_run_checkpoint(audit, jobs, states, provider_stops)
                members = [pairs[key] for key in job["pair_ids"]]
                system, user, refs = build_case_prompts(members, phase, members[0]["passages"] if phase == "B" else None,
                                                       case_protocol=job.get("case_protocol", "requirement_declare_v1"))
                request = p.build_request(providers[provider], system, user, config["max_output_tokens"])
                if p.digest(request) != job["request_sha256"]:
                    raise ValueError("Generated case request differs from its frozen hash.")
                number = int(directories[-1].name[8:]) + 1 if directories else 1
                directory = audit / "requests" / job["job_id"] / f"attempt_{number:03d}"
                directory.mkdir(parents=True, exist_ok=False)
                p.write_json(directory / "request.json", request)
                p.write_json(directory / "started.json", {"job_id": job["job_id"], "created_utc": p.utc_now(),
                                                           "attempt": number, "explicit_retry": bool(directories)})
                print(f"{provider} | phase {phase} | {job['case_id']} | {len(members)} rules | attempt {number}", flush=True)
                response = p.send_request(request, timeout_seconds=config["timeout_seconds"])
                response["received_utc"] = p.utc_now()
                p.write_json(directory / "response.json", response)
                if response["status"] == "ok":
                    result = parse_case_assessment(response.get("text", ""), job["rule_ids"], refs,
                                                   case_protocol=job.get("case_protocol", "requirement_declare_v1"))
                else:
                    result = {"status": response["status"], "assessments": [], "error": response.get("error")}
                result.update({"job_id": job["job_id"], "request_sha256": job["request_sha256"],
                               "completed_utc": p.utc_now(), "call_attempted": response.get("attempted"),
                               "model_returned": response.get("model_returned"), "usage": response.get("usage"),
                               "elapsed_seconds": response.get("elapsed_seconds"), "http_status": response.get("http_status"),
                               "raw_response_sha256": p.sha256_file(directory / "response.json")})
                result.update(normalise_usage(job["provider"], response.get("usage")))
                p.write_json(directory / "assessment.json", result)
                with counter_lock:
                    counts["processed"] += 1
                    counts["successful" if result["status"] == "ok" else "failed"] += 1
                    states[job["job_id"]] = result["status"]
                    stop_info = classify_provider_stop(job["provider"], response)
                    if stop_info is not None:
                        blocked.add(provider)
                        provider_stops[provider] = stop_info
                        print(f"  {provider}: {stop_info['reason']}; progress saved. Restore the provider's "
                              "availability, then use the resume command with the same frozen configuration.", flush=True)
                    write_run_checkpoint(audit, jobs, states, provider_stops)
                    print(f"  {provider} | phase {phase} | {job['case_id']}: {result['status']} "
                          f"({counts['processed']}/{len(jobs)} requests)", flush=True)
                    if result["status"] != "ok":
                        print(f"  Details: {p.canonical(result.get('error'))}", flush=True)

        def guarded_process(provider: str, phase: str, stream: int) -> None:
            try:
                process(provider, phase, stream)
            except BaseException:
                stop.set()
                raise

        try:
            # Independent provider streams; phase B never receives phase A answers.
            for phase in config["conditions"]:
                selected = [provider for provider in providers if provider_id is None or provider == provider_id]
                workers = min(config["request_workers"], len(selected) * config["requests_per_provider"])
                with ThreadPoolExecutor(max_workers=workers) as executor:
                    futures = [executor.submit(guarded_process, provider, phase, stream)
                               for provider in selected for stream in range(config["requests_per_provider"])]
                    for future in futures:
                        future.result()
                _analyse(output, audit, config)
        finally:
            counts["blocked_providers"] = len(blocked)
            for job in jobs:
                if states.get(job["job_id"]) == "running":
                    directories = p._attempt_directories(audit, job)
                    states[job["job_id"]] = (read_case_attempt(audit, job, directories[-1])["status"]
                                              if directories else "pending")
            write_run_checkpoint(audit, jobs, states, provider_stops, invocation_finished=True)
            _analyse(output, audit, config)
    return counts
