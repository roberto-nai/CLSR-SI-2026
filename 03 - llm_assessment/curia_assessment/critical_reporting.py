"""Report an integrated critical study without mixing case and rule endpoints.

The caller holds the parent study lock. Both native stages must be idle. Native
case reviewer files are linked, never regenerated or copied by this module.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import ExitStack
import csv
import hashlib
import io
import json
import math
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any

from . import pipeline as p
from .event_log_statistics import format_event_log_statistics, write_event_log_statistics
from .metrics import normalise_usage

LEGAL = {"compliance", "violation", "undetermined"}
TRACE = {"compliance", "violation"}
ADEQUACY = {"Agreement", "Disagreement", "Undetermined"}
HEADS = {"in_scope_assessment": "in_scope", "ungated_assessment": "ungated"}
EVENT_LOG_FILES = ("event_log_statistics.json", "event_log_statistics.csv", "event_log_statistics.txt")
RESOURCE_FIELDS = ("stage", "provider_id", "provider", "model", "planned_requests", "completed_requests",
                   "retained_attempts", "recorded_api_calls", "unknown_call_attempt_flags", "input_tokens", "output_tokens",
                   "total_tokens", "measured_input_attempts", "measured_output_attempts", "measured_total_attempts",
                   "api_seconds_total", "api_seconds_mean", "measured_elapsed_attempts")
AUDIT_FIELDS = ("rule_id", "model", "provider_id", "status", "declare", "requirement",
                "first_status", "first_in_scope", "latest_in_scope", "in_scope_relation", "in_scope_justification",
                "in_scope_formula_consistent_D", "first_ungated", "latest_ungated", "ungated_relation", "ungated_justification",
                "ungated_formula_consistent_D", "logical_flags", "format_notes", "scope_conditions", "mapping_assumptions", "missing_information",
                "counterexamples", "witness_checks", "text_review_status", "error", "job_id", "expert_outcome", "expert_notes")


def _require(value, message):
    if not value:
        raise ValueError(message)


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _csv(path):
    if not path.is_file():
        return []
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        _require(reader.fieldnames and len(reader.fieldnames) == len(set(reader.fieldnames)), f"Malformed CSV header: {path}")
        rows = list(reader)
    _require(all(None not in row and all(v is not None for v in row.values()) for row in rows), f"Malformed CSV rows: {path}")
    return rows


def _csv_bytes(rows, fields):
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n", extrasaction="raise")
    writer.writeheader(); writer.writerows(rows)
    return ("\ufeff" + output.getvalue()).encode()


def _inside(root, value):
    relative = Path(value)
    _require(not relative.is_absolute() and ".." not in relative.parts, "Study paths must remain inside the new study.")
    result = (root / relative).resolve()
    _require(result.is_relative_to(root), "Study path escaped its parent directory.")
    return result


def _known_number(value):
    if value in (None, ""):
        return None
    _require(not isinstance(value, bool), "A boolean is not a resource measurement.")
    result = float(value)
    _require(math.isfinite(result) and result >= 0, "Invalid resource measurement.")
    return result


def _resources(stage, providers, jobs, ledger):
    result = []
    for provider in providers:
        members = [r for r in ledger if r["provider_id"] == provider["id"]]
        allocated = [j for j in jobs if j["provider_id"] == provider["id"]]
        row = dict(stage=stage, provider_id=provider["id"], provider=provider["provider"], model=provider["model"],
                   planned_requests=len(allocated), completed_requests=sum(j.get("complete", False) for j in allocated),
                   retained_attempts=len(members), recorded_api_calls=sum(r.get("call_attempted") is True for r in members),
                   unknown_call_attempt_flags=sum(r.get("call_attempted") is None for r in members))
        for field, prefix in (("input_tokens", "input"), ("output_tokens", "output"), ("total_tokens", "total")):
            values = [r.get(field) for r in members if r.get(field) is not None]
            row[field] = sum(values) if values else None if members else 0
            row["measured_" + prefix + "_attempts"] = len(values)
        elapsed = [_known_number(r.get("elapsed_seconds")) for r in members]
        elapsed = [v for v in elapsed if v is not None]
        row.update(api_seconds_total=round(math.fsum(elapsed),6) if elapsed else None if members else 0,
                   api_seconds_mean=sum(elapsed)/len(elapsed) if elapsed else None, measured_elapsed_attempts=len(elapsed))
        result.append(row)
    return result


def _combined_resources(stage_rows):
    combined = []
    groups = defaultdict(list)
    for row in stage_rows:
        groups[row["provider_id"], row["model"]].append(row)
    sums = ("planned_requests", "completed_requests", "retained_attempts", "recorded_api_calls", "unknown_call_attempt_flags",
            "measured_input_attempts", "measured_output_attempts", "measured_total_attempts", "measured_elapsed_attempts")
    for (provider, model), rows in sorted(groups.items()):
        row = dict(stage="combined", provider_id=provider, provider=rows[0]["provider"], model=model)
        row.update({field: sum(r[field] for r in rows) for field in sums})
        for field in ("input_tokens", "output_tokens", "total_tokens", "api_seconds_total"):
            known = [r[field] for r in rows if r[field] is not None]
            measurement = {"input_tokens":"measured_input_attempts","output_tokens":"measured_output_attempts",
                           "total_tokens":"measured_total_attempts","api_seconds_total":"measured_elapsed_attempts"}[field]
            row[field] = sum(known) if row[measurement] else None if row["retained_attempts"] else 0
        row["api_seconds_mean"] = row["api_seconds_total"]/row["measured_elapsed_attempts"] if row["measured_elapsed_attempts"] else None
        combined.append(row)
    return combined


def _case_archive(case_dir, jobs, providers):
    from .case_prompts import build_case_prompts, parse_case_assessment
    first, latest, ledger, completed = {}, {}, [], []
    audit = case_dir / "_audit"
    pairs = p.read_jsonl(audit / "prepared/pairs.jsonl")
    pair_index = {r["pair_id"]:r for r in pairs}
    provider_index = {r["id"]:r for r in providers}
    for original in jobs:
        job = dict(original)
        _require(job.get("case_protocol") == "critical_v1", "A native case job uses another protocol.")
        members = [pair_index[key] for key in job["pair_ids"]]
        system,user,refs = build_case_prompts(members,job["condition"],case_protocol="critical_v1")
        expected = p.build_request(provider_index[job["provider_id"]],system,user,32000)
        _require(p.digest(expected) == job["request_sha256"], "The native case request cannot be reproduced from frozen inputs.")
        records = []
        for directory in p._attempt_directories(audit, job):
            saved = p.read_json(directory / "assessment.json") if (directory / "assessment.json").is_file() else {"status": "unavailable"}
            response_path = directory / "response.json"
            raw = p.read_json(response_path) if response_path.is_file() else {}
            request_path = directory / "request.json"
            _require(request_path.is_file() and p.read_json(request_path) == expected, "A case request differs from its frozen job.")
            _require((directory/"started.json").is_file() and p.read_json(directory/"started.json").get("job_id") == job["job_id"], "Case attempt start belongs to another job.")
            if (directory / "assessment.json").is_file():
                _require(response_path.is_file(), "A saved case judgement has no raw response.")
                _require(saved.get("raw_response_sha256") == p.sha256_file(response_path), "A fresh critical case assessment lacks its correct raw-response hash.")
                _require(all(saved.get(field) == raw.get(remote) for field,remote in (("usage","usage"),("elapsed_seconds","elapsed_seconds"),("call_attempted","attempted"))), "Saved case resource metadata differs from its raw response.")
                if raw.get("status") == "ok":
                    parsed = parse_case_assessment(raw.get("text"),job["rule_ids"],refs,case_protocol="critical_v1")
                    _require(all(saved.get(field) == parsed.get(field) for field in ("status","assessments","error","format_notes","diagnostic_flags")), "Saved case judgements differ from strict critical replay.")
                    _require(raw.get("model_returned") == job["model"], "Case response names another model.")
                else:
                    _require(saved.get("status") == raw.get("status") and saved.get("assessments") == [], "A provider failure became a case judgement.")
            ledger.append({**job, "call_attempted": raw.get("attempted"), "elapsed_seconds": raw.get("elapsed_seconds"),
                           **normalise_usage(job["provider"], raw.get("usage"))})
            records.append(saved)
        if records:
            first[job["job_id"]], latest[job["job_id"]] = records[0], records[-1]
        job["complete"] = bool(records and records[-1].get("status") == "ok")
        completed.append(job)
    return first, latest, ledger, completed


def _leaf_counts(jobs, results, formal, *, reported=False):
    summaries = []
    for provider_id in sorted({j["provider_id"] for j in jobs}):
        members = [j for j in jobs if j["provider_id"] == provider_id]
        legal, trace, comparison, trace_comparison = Counter(), Counter(), Counter(), Counter()
        valid_leaves = 0
        planned = sum(len(j.get("rule_ids", [])) for j in members)
        for job in members:
            rows = results.get(job["job_id"], {}).get("assessments", [])
            ids = [r.get("rule_id") for r in rows]
            if len(ids) != len(set(ids)) or set(ids) != set(job.get("rule_ids", [])):
                continue
            for row in rows:
                valid_leaves += row.get("status") == "ok"
                requirement = row.get("requirement_assessment")
                outcome = requirement.get("outcome") if reported and isinstance(requirement,dict) else row.get("outcome")
                if (reported or row.get("status") == "ok") and isinstance(outcome,str) and outcome in LEGAL:
                    legal[outcome] += 1
                    reference = formal.get((job["case_id"], row["rule_id"]))
                    category = "Undetermined" if outcome == "undetermined" or reference is None else "Agreement" if (outcome == "compliance") == bool(reference) else "Disagreement"
                    comparison[category] += 1
                head = row.get("trace_conformance")
                if (reported or row.get("status") == "ok") and isinstance(head, dict) and isinstance(head.get("outcome"),str) and head["outcome"] in TRACE:
                    trace[head["outcome"]] += 1
                    reference = formal.get((job["case_id"], row["rule_id"]))
                    trace_comparison["Undetermined" if head["outcome"] == "undetermined" or reference is None else
                                     "Agreement" if (head["outcome"] == "compliance") == bool(reference) else "Disagreement"] += 1
        summaries.append(dict(provider_id=provider_id, model=members[0]["model"], planned=planned, requirement=dict(legal),
                              trace=dict(trace), comparison=dict(comparison), trace_comparison=dict(trace_comparison), unavailable=planned-sum(legal.values()),
                              trace_unavailable=planned-sum(trace.values()), structurally_valid_leaves=valid_leaves))
    return summaries


def _case_comparison(cases, providers, native):
    by_key = {}
    for row in native:
        key = row["case_id"], row["provider_id"]
        _require(key not in by_key, "Repeated native case/model result.")
        by_key[key] = row
    rows = []
    for case in cases:
        row = dict(case_id=case["case_id"], case_year=case["case_year"])
        for provider in providers:
            value = by_key.get((case["case_id"], provider["id"]), {})
            completed = int(value.get("n_completed", "0"))
            flag = value.get("violation", "")
            row[provider["id"] + "_model"] = provider["model"]
            row[provider["id"] + "_case_flag"] = flag if flag == "yes" or completed == 19 else "unavailable"
            row[provider["id"] + "_status"] = value.get("status", "pending")
            row[provider["id"] + "_completed_rules"] = completed
            row[provider["id"] + "_violated_rule_ids"] = value.get("violated_rule_ids", "[]")
            row[provider["id"] + "_case_explanations"] = value.get("explanations", "No recorded model response; this is an operationally unavailable result.")
        row.update(case_details="case_assessment/case_results.csv", rule_details="case_assessment/rule_assessments.csv",
                   formalisation_details="formalisation_results.csv")
        rows.append(row)
    return rows


def _verified_case_rows(jobs,latest):
    rows = []
    for job in jobs:
        result = latest.get(job["job_id"],{})
        leaves = result.get("assessments",[])
        ids = [r.get("rule_id") for r in leaves]
        allocated = len(ids) == len(set(ids)) and set(ids) == set(job["rule_ids"])
        valid = [r for r in leaves if r.get("status") == "ok" and isinstance(r.get("outcome"),str) and r["outcome"] in LEGAL] if allocated else []
        counts = Counter(r["outcome"] for r in valid)
        flag = "yes" if counts["violation"] else "no" if counts["compliance"] == 19 else "undetermined" if len(valid) == 19 else "unavailable"
        explanations = "\n".join(f"{r.get('rule_id','unknown rule')}: {r.get('outcome') or r.get('status','unavailable')} — {r.get('justification') or r.get('error') or 'No assessable model explanation.'}"
                                 for r in leaves) if allocated else "No complete, unambiguous recorded rule allocation; model results are operationally unavailable."
        reference = "Complete requirements, formulas, outcomes and explanations: case_assessment/rule_assessments.csv; original responses: case_assessment/_audit/requests/."
        suffix = "\n[Faithful excerpt truncated at 24,000 characters.]\n" + reference
        explanations = explanations + "\n\n" + reference if len(explanations)+len(reference)+2 <= 24000 else explanations[:24000-len(suffix)] + suffix
        rows.append(dict(case_id=job["case_id"],provider_id=job["provider_id"],n_completed=len(valid),violation=flag,
                         status="completed" if len(valid)==19 else "partial" if valid else "pending" if not result else "unavailable",
                         explanations=explanations,
                         violated_rule_ids=_json(sorted(r["rule_id"] for r in valid if r["outcome"]=="violation"))))
    return rows


def _audit_data(audit_dir):
    from .formalisation_runner import AuditRunner
    from .formalisation_witnesses import check_job
    runner = AuditRunner(audit_dir)
    runner.verify_frozen()
    rule_rows = p.read_jsonl(audit_dir / "_audit/prepared/rules.jsonl")
    rules = {r.get("rule_id", r.get("base_rule_id")): r for r in rule_rows}
    _require(len(rules) == 19, "The formalisation stage requires nineteen distinct rules.")
    rows, ledger, jobs, details = [], [], [], []
    engine_cache = {}
    for original in runner.jobs:
        job = dict(original)
        records = [runner.read_attempt(job, path, write_missing=False) for path in runner.attempt_directories(job)]
        ledger.extend(records)
        first = records[0] if records else {}
        latest = records[-1] if records else {}
        job["complete"] = latest.get("status") == "ok"
        jobs.append(job)
        rule = rules[job["rule_id"]]
        check = (check_job(job, rule, latest, set(runner.protocol["activity_labels"]), engine_cache=engine_cache)
                 if job["complete"] else {"witnesses": [], "in_scope_formula_consistent_D": False, "ungated_formula_consistent_D": False})
        row = dict(rule_id=job["rule_id"], model=job["model"], provider_id=job["provider_id"], status=latest.get("status", "pending"), first_status=first.get("status","pending"),
                   declare=rule["declare"], requirement=rule["requirement"], job_id=job["job_id"],
                   logical_flags=_json(latest.get("logical_flags", [])), format_notes=_json(latest.get("format_notes", [])), scope_conditions=_json(latest.get("scope_conditions", [])),
                   mapping_assumptions=_json(latest.get("mapping_assumptions", [])), missing_information=_json(latest.get("missing_information", [])),
                   counterexamples=_json(latest.get("counterexamples", [])), witness_checks=_json(check["witnesses"]), text_review_status="pending",
                   error=_json(latest.get("error")) if latest.get("error") else "", expert_outcome="", expert_notes="")
        for head, prefix in HEADS.items():
            latest_head, first_head = latest.get(head), first.get(head)
            latest_head = latest_head if job["complete"] and isinstance(latest_head, dict) else {}
            raw_first = first.get("raw_json")
            if isinstance(raw_first,dict):
                first_head = raw_first.get(head)
            first_head = first_head if isinstance(first_head, dict) else {}
            row.update({"first_" + prefix: first_head.get("outcome") if isinstance(first_head.get("outcome"),str) and first_head["outcome"] in ADEQUACY else "",
                        "latest_" + prefix: latest_head.get("outcome") if isinstance(latest_head.get("outcome"),str) and latest_head["outcome"] in ADEQUACY else "",
                        prefix + "_relation": latest_head.get("relation", ""), prefix + "_justification": latest_head.get("justification", ""),
                        prefix + "_formula_consistent_D": "true" if check[prefix + "_formula_consistent_D"] else "false"})
        rows.append(row)
        examples = latest.get("counterexamples")
        for index, witness in enumerate(examples if isinstance(examples, list) else [], 1):
            if not isinstance(witness, dict):
                continue
            for field, value in witness.items():
                values = value if isinstance(value, list) else [value]
                for item in values:
                    text = item if isinstance(item, str) else _json(item)
                    for start in range(0, max(1, len(text)), 1000):
                        details.append(dict(rule_id=job["rule_id"], model=job["model"], witness_index=index, component=field,
                                            value=text[start:start+1000]))
        for index, witness in enumerate(check["witnesses"], 1):
            details.append(dict(rule_id=job["rule_id"], model=job["model"], witness_index=index,
                                component="machine checks (not textual truth)", value=_json({k:v for k,v in witness.items() if k not in {"model_counterexample", "engine_check"}})))
    runner.verify_frozen()
    rows.sort(key=lambda r: (r["rule_id"], r["model"]))
    return rows, ledger, jobs, details


def _duration(value):
    if value is None:
        return "unavailable"
    return f"{int(value//3600)} h {(value%3600)/60:.1f} min" if value >= 3600 else f"{value/60:.2f} min"


def _audit_summary(rows):
    result = []
    for provider in sorted({r["provider_id"] for r in rows}):
        members = [r for r in rows if r["provider_id"] == provider]
        for prefix in HEADS.values():
            first = Counter(r["first_" + prefix] for r in members)
            latest = Counter(r["latest_" + prefix] for r in members)
            result.append(dict(provider_id=provider, model=members[0]["model"], domain=prefix,
                               planned=len(members), first={o:first[o] for o in sorted(ADEQUACY)},
                               latest={o:latest[o] for o in sorted(ADEQUACY)},
                               first_unavailable=first[""], latest_unavailable=latest[""],
                               formula_consistent_D=sum(r[prefix + "_formula_consistent_D"] == "true" for r in members),
                               text_review_status="pending"))
    return result


def _report(complete, first, latest, audit, resources, case_rows, event_log_statistics=None):
    lines = ["CRITICAL PHASE 1 STUDY", "COMPLETE" if complete else "PARTIAL — operationally unavailable results remain separate from semantic uncertainty.", "",
             "This study separates assessments of 50 CaseID clusters from a formalisation audit of the same 19 rules by three models. The planned allocation is 150 case requests and 57 rule-audit requests: 207 API requests before technical retries. Nineteen case–rule judgements within one case response are not nineteen API calls.",
             "Case prompts supply metadata, the full ordered event log, the literal textual requirement and its DECLARE formula. Each leaf retains a closed-observed-trace judgement and a scope-sensitive requirement assessment. Case exports and case–checker Agreement/Disagreement/Undetermined use the requirement assessment only. A formalisation Disagreement does not turn an actual case's Undetermined outcome into a violation or a checker disagreement."]
    if event_log_statistics is not None:
        lines += ["", "EVENT-LOG CHARACTERISTICS", format_event_log_statistics(event_log_statistics)]
    lines += ["", "CASE ASSESSMENTS"]
    first_by = {r["provider_id"]:r for r in first}
    for row in latest:
        before = first_by[row["provider_id"]]
        a, f = row["requirement"], before["requirement"]
        c, trace, tc = row["comparison"], row["trace"], row["trace_comparison"]
        lines.append(f"{row['model']}: latest requirement compliance/violation/undetermined {a.get('compliance',0)}/{a.get('violation',0)}/{a.get('undetermined',0)} out of {row['planned']}; operationally unavailable {row['unavailable']}. First-attempt reported labels were {f.get('compliance',0)}/{f.get('violation',0)}/{f.get('undetermined',0)}, unavailable {before['unavailable']}; structurally valid leaves {before['structurally_valid_leaves']}/{before['planned']}. Reported-label availability is not structural validity. Latest case–checker Agreement/Disagreement/Undetermined {c.get('Agreement',0)}/{c.get('Disagreement',0)}/{c.get('Undetermined',0)} among valid requirement judgements. Closed-trace compliance/violation/undetermined {trace.get('compliance',0)}/{trace.get('violation',0)}/{trace.get('undetermined',0)}, unavailable {row['trace_unavailable']}.")
        lines.append(f"Validated closed-trace–checker Agreement/Disagreement/Undetermined: {tc.get('Agreement',0)}/{tc.get('Disagreement',0)}/{tc.get('Undetermined',0)}. This formula-evaluation comparison is separate from both requirement–checker comparison and rule-level formalisation adequacy.")
    lines += ["The 50-row case_comparison.csv links strictly replayed model case outcomes and native detail/reviewer files. A positive flag means at least one model-reported violation passing structural validation, not independently established legal truth; diagnostic flags still require review. A negative flag requires complete model-reported compliance. Partial unavailable cases are not invented semantic Undetermined judgements.", "", "FORMALISATION AUDIT — A DIFFERENT UNIT"]
    for row in audit:
        a, b = row["latest"], row["first"]
        lines.append(f"{row['model']} | {row['domain']}: latest reported Agreement/Disagreement/Undetermined {a['Agreement']}/{a['Disagreement']}/{a['Undetermined']} out of {row['planned']}; unavailable {row['latest_unavailable']}. First-attempt counts {b['Agreement']}/{b['Disagreement']}/{b['Undetermined']}, unavailable {row['first_unavailable']}. Formula-consistent hypothetical Disagreements {row['formula_consistent_D']}; independent textual and expert review pending.")
    lines += ["The primary audit conditions on stated applicability; it does not assume away fulfilment, content, identity or service facts. The secondary ungated audit separately tests scope overreach. Synthetic base-act completeness is a logical fairness convention, not evidence that actual CURIA logs are complete. Machine witness checks test formula truth and direction only; they do not validate hypothetical world facts or textual/legal correctness. Semantic diagnostic flags are retained rather than treated as technical failures to be resampled. No target disagreement rate or majority-vote ground truth is used.", "", "RESOURCES — ALL RETAINED ATTEMPTS"]
    for row in resources:
        n = row["retained_attempts"]
        token = lambda field: "unavailable" if row[field] is None else f"{row[field]:,}"
        lines.append(f"{row['stage']} | {row['model']}: completed requests {row['completed_requests']}/{row['planned_requests']}; retained attempts {n}, recorded API calls {row['recorded_api_calls']}, unknown call flags {row['unknown_call_attempt_flags']}. Known input/output/total tokens {token('input_tokens')}/{token('output_tokens')}/{token('total_tokens')}; coverage {row['measured_input_attempts']}/{n}, {row['measured_output_attempts']}/{n}, {row['measured_total_attempts']}/{n}. Cumulative API time {_duration(row['api_seconds_total'])}; measured mean {_duration(row['api_seconds_mean'])}; elapsed coverage {row['measured_elapsed_attempts']}/{n}.")
    lines += ["Unknown measurements remain unavailable, not zero. Failed attempts and retries are included once. Combined resource rows add the two native stages, never the expanded 19 rule rows. Concurrent request durations do not equal study wall time and do not measure model CPU time.", "",
              "Interpretation: the case observations are dependent within 50 case clusters; audit codings are dependent within 19 rules. The two endpoints have different denominators and are not interchangeable measures of legal accuracy. First attempts and latest technical recoveries are shown separately without selecting responses for disagreement. Native case expert CSV/XLSX files remain independent, with no fabricated expert ratings; review_index.txt links them and the audit workbook. No case verdict is derived from a hypothetical witness."]
    return "\n\n".join(lines) + "\n"


def _workbook(staging, rows, summaries, details):
    from .expert_workbooks import _runtime
    node, modules = _runtime()
    (staging / "node_modules").symlink_to(modules, target_is_directory=True)
    helper = staging / "critical_reporting.mjs"
    shutil.copyfile(Path(__file__).with_suffix(".mjs"), helper)
    options = dict(rows=rows, fields=AUDIT_FIELDS, summaries=summaries, details=details,
                   output=str(staging / "formalisation_results.xlsx"), previews=str(staging / "previews"))
    manifest = staging / "workbook.json"
    manifest.write_text(_json(options), encoding="utf-8")
    result = subprocess.run([str(node), str(helper), str(manifest)], capture_output=True, text=True, timeout=240)
    _require(result.returncode == 0, "Critical audit workbook failed: " + result.stderr[-2500:])
    _require((staging / "formalisation_results.xlsx").is_file(), "Critical audit workbook was not exported.")


def _snapshot(study_dir,case_dir,audit_dir,config_path):
    paths = {study_dir/"study.json",config_path}
    for stage in (case_dir,audit_dir):
        audit = stage/"_audit"
        paths.update(path for name in ("run_manifest.json","manifest.json","protocol.json","recovery_plan.json") if (path:=audit/name).is_file())
        for name in ("prepared","requests"):
            directory = audit/name
            if directory.exists():
                for path in directory.rglob("*"):
                    _require(not path.is_symlink(),"Scientific report inputs cannot be symbolic links.")
                    if path.is_file() and path.name != ".DS_Store":
                        paths.add(path)
    return {str(path.relative_to(study_dir)):p.sha256_file(path) for path in sorted(paths)}


def write_critical_outputs(study_dir: Path) -> dict[str, Any]:
    """Write parent-level results after both native stages are idle.

    Partial studies retain planned rows and operational unavailability. The
    caller serialises parent mutations with its study lock. Existing manually
    edited generated files are preserved by a private publication hash guard.
    """
    study_dir = Path(study_dir).resolve()
    study = p.read_json(study_dir/"study.json")
    stages = [_inside(study_dir,study[key]) for key in ("case_assessment_dir","formalisation_audit_dir")]
    _require(all(not (directory/"_audit/.assessment.lock").exists() for directory in stages), "Native stages must be idle before integrated reporting.")
    with ExitStack() as locks:
        identities = {}
        for stage in stages:
            locks.enter_context(p.output_lock(stage/"_audit"))
            path = stage/"_audit/.assessment.lock"
            stat = path.stat()
            identities[path] = (stat.st_dev,stat.st_ino)
        return _write_locked(study_dir,identities)


def _write_locked(study_dir,lock_identities):
    study = p.read_json(study_dir / "study.json")
    _require(study.get("schema_version") == 1 and study.get("protocol_id") == "critical_phase1_v1"
             and study.get("case_protocol") == "critical_v1" and study.get("phase") == 1,
             "Unknown integrated critical study protocol.")
    _require((study.get("planned_cases"), study.get("rules"), study.get("planned_case_requests"), study.get("planned_audit_requests")) == (50,19,150,57),
             "Unexpected critical-study allocation.")
    case_dir = _inside(study_dir, study["case_assessment_dir"])
    audit_dir = _inside(study_dir, study["formalisation_audit_dir"])
    config_path = _inside(study_dir, study["case_config"])
    inputs = _snapshot(study_dir,case_dir,audit_dir,config_path)
    code_sha256 = p.code_digest()
    config = p.read_json(config_path)
    providers = config["providers"]
    _require(len(providers) == 3 and len({r["id"] for r in providers}) == 3, "Expected three distinct models.")
    cases = _csv(case_dir / "_audit/prepared/selected_cases.csv")
    _require(len(cases) == len({r["case_id"] for r in cases}) == 50, "The frozen 50-case cohort is missing or changed.")
    event_records = p.read_jsonl(case_dir / "_audit/prepared/pairs.jsonl")
    _require({record["case_id"] for record in event_records} == {case["case_id"] for case in cases},
             "The frozen event-log cohort differs from the selected cases.")
    jobs = p.read_jsonl(case_dir / "_audit/prepared/planned_assessments.jsonl")
    _require(len(jobs) == len({j["job_id"] for j in jobs}) == 150, "The native case plan is incomplete.")
    _require({(j["case_id"],j["provider_id"]) for j in jobs} == {(c["case_id"],m["id"]) for c in cases for m in providers}
             and all(len(j.get("rule_ids",[])) == len(set(j["rule_ids"])) == 19 for j in jobs), "The case/model/rule allocation differs from the study.")
    if config.get("case_ids") is not None:
        _require(set(config["case_ids"]) == {c["case_id"] for c in cases}, "The frozen case cohort differs from its configuration.")
    first, latest, case_ledger, case_jobs = _case_archive(case_dir, jobs, providers)
    formal_rows = p.read_jsonl(case_dir / "_audit/prepared/formal_results.jsonl")
    formal = {(r["case_id"], r.get("base_rule_id",r.get("rule_id"))):r["formal_satisfied"] for r in formal_rows if r.get("formal_satisfied") in (True,False,0,1)}
    first_summary, latest_summary = _leaf_counts(jobs,first,formal,reported=True), _leaf_counts(jobs,latest,formal)
    verified_cases = _verified_case_rows(case_jobs,latest)
    case_rows = _case_comparison(cases,providers,verified_cases)
    audit_rows, audit_ledger, audit_jobs, details = _audit_data(audit_dir)
    _require(len(audit_rows) == 57 and {(r["provider_id"],r["model"]) for r in audit_rows} == {(r["id"],r["model"]) for r in providers}, "Audit models/allocation differ from case assessment.")
    case_rules = {r.get("base_rule_id",r.get("rule_id")):r for r in _csv(case_dir / "_audit/prepared/selected_rules.csv")}
    _require(all(r["rule_id"] in case_rules and all(r[field] == case_rules[r["rule_id"]][field] for field in ("declare","requirement")) for r in audit_rows), "Case and audit literal rules differ.")
    summary = _audit_summary(audit_rows)
    resources = _resources("case_assessment",providers,case_jobs,case_ledger) + _resources("formalisation_audit",providers,audit_jobs,audit_ledger)
    resources += _combined_resources(resources)
    complete = (sum(j["complete"] for j in case_jobs) == 150 and sum(j["complete"] for j in audit_jobs) == 57
                and all(r["unavailable"] == r["trace_unavailable"] == 0 for r in latest_summary)
                and len(verified_cases) == 150 and all(int(r["n_completed"]) == 19 for r in verified_cases))
    audit = study_dir / "_audit"; audit.mkdir(exist_ok=True)
    state_path = audit / "critical_report_state.json"
    previous = p.read_json(state_path) if state_path.exists() else None
    base_names = ("case_comparison.csv","formalisation_results.csv","formalisation_results.xlsx","study_resources.csv","study_report.txt","review_index.txt", *EVENT_LOG_FILES)
    copies = {f"expert_review/expert_{i}_audit.{ext}": f"formalisation_results.{ext}" for i in (1,2) for ext in ("csv","xlsx")}
    names = (*base_names,*copies)
    def guard():
        for name in base_names:
            path = study_dir / name
            expected = (previous or {}).get("published_sha256", {}).get(name)
            _require(not path.exists() and not path.is_symlink() if expected is None else path.is_file() and p.sha256_file(path) == expected,
                     "A parent report was manually edited or is untracked; preserve it: " + name)
    guard()
    links = ["EXPERT AND RESEARCH REVIEW", "Case expert files retain their native annotations and controls. Model columns are visible; automatic model blinding is not claimed."]
    for number in (1,2):
        for extension in ("csv","xlsx"):
            relative = Path(study["case_assessment_dir"]) / f"expert_review/expert_{number}.{extension}"
            links.append(f"{relative.as_posix()}" + ("" if (study_dir/relative).is_file() else " — not yet exported"))
    links += ["formalisation_results.csv", "formalisation_results.xlsx", *EVENT_LOG_FILES,
              "formalisation_audit/_audit/protocol.json — frozen semantics, activity dictionary and assessment domains", *copies,
              "Each audit reviewer has an independent blank copy. expert_outcome means the PRIMARY in-scope adequacy judgement: Agreement | Disagreement | Undetermined. Use expert_notes for the reasons and any secondary ungated assessment. No human rating is pre-filled.",
              "The audit workbook is a research review view, not an expert-labelled ground truth. Textual and legal expert review remains pending."]
    with tempfile.TemporaryDirectory(prefix="critical-report-",dir=audit) as directory:
        staging = Path(directory)
        event_statistics = write_event_log_statistics(staging, event_records)
        _require(event_statistics["n_cases"] == len(cases), "The event-log statistics do not describe the frozen case cohort.")
        _workbook(staging,audit_rows,summary,details)
        data = {"case_comparison.csv":_csv_bytes(case_rows,case_rows[0].keys()), "formalisation_results.csv":_csv_bytes(audit_rows,AUDIT_FIELDS),
                "formalisation_results.xlsx":(staging/"formalisation_results.xlsx").read_bytes(), "study_resources.csv":_csv_bytes(resources,RESOURCE_FIELDS),
                "study_report.txt":_report(complete,first_summary,latest_summary,summary,resources,case_rows,
                                          event_log_statistics=event_statistics).encode(),
                "review_index.txt":b""}
        data.update({name: (staging/name).read_bytes() for name in EVENT_LOG_FILES})
        _require(all(path.is_file() and (path.stat().st_dev,path.stat().st_ino)==identity for path,identity in lock_identities.items()), "A native report lock changed ownership.")
        _require(inputs == _snapshot(study_dir,case_dir,audit_dir,config_path) and code_sha256 == p.code_digest(), "Scientific inputs or executable source changed during reporting.")
        guard()
        expert_files = {}
        for target,source in copies.items():
            path = study_dir/target
            current_sha = p.sha256_file(path) if path.is_file() else None
            prior = (previous or {}).get("expert_review_files",{}).get(target,{})
            generated_sha = (previous or {}).get("published_sha256",{}).get(target)
            preserve = current_sha is not None and (prior.get("preserved_human_edits") is True or current_sha != generated_sha)
            expert_files[target] = dict(preserved_human_edits=preserve,
                                       reviewed_machine_csv_sha256=prior.get("reviewed_machine_csv_sha256") if preserve else hashlib.sha256(data["formalisation_results.csv"]).hexdigest())
            if preserve:
                links.append(target + " — human-edited copy preserved; it may refer to an earlier model-response snapshot. Current research results are in formalisation_results.csv.")
            else:
                data[target] = data[source]
        data["review_index.txt"] = ("\n".join(links)+"\n").encode()
        for name,value in data.items():
            temporary = staging / name; temporary.parent.mkdir(parents=True,exist_ok=True); temporary.write_bytes(value)
            (study_dir/name).parent.mkdir(parents=True,exist_ok=True); temporary.replace(study_dir/name)
        previews = audit / "critical_previews"; previews.mkdir(exist_ok=True)
        for source in (staging/"previews").iterdir():
            shutil.copyfile(source,previews/source.name)
    state = dict(schema_version=1,status="complete_ready_for_visual_review" if complete else "partial_ready_for_visual_review",
                 case_requests_complete=sum(j["complete"] for j in case_jobs), audit_requests_complete=sum(j["complete"] for j in audit_jobs),
                 independent_expert_labels_fabricated=False,manual_visual_review_completed=False,
                 input_sha256=inputs,code_sha256=code_sha256,
                 expert_review_files=expert_files,
                 published_sha256={name:p.sha256_file(study_dir/name) for name in names})
    p.write_json(state_path,state)
    return state
