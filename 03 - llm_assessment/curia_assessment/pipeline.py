"""Prepare immutable comparisons, run explicit API attempts and report outcomes."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import platform
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from . import __version__
from .comparison import compare_rows, summarise_comparisons
from .case_documents import index_case_documents
from .formal import compute_formal_results, import_formal_results
from .inputs import load_cases, load_catalogue, sha256_file, validate_year_range
from .paths import display_path
from .metrics import TOKEN_FIELDS, normalise_usage, summarise_resource_usage
from .prompts import build_prompts, parse_assessment
from .providers import build_request, format_error, send_request
from .reporting import write_case_extraction_report
from .sampling import cross_pairs, select_cases_by_year, select_requirements
from .event_log_statistics import write_event_log_statistics

DEFAULTS = {
    "formal_mode": "import", "formal_results": None, "output_dir": "outputs/direct",
    "min_events": 5, "case_category": "C", "country": "EN", "case_ids": None,
    "year_start": 2017, "year_end": 2026, "cases_per_year": 5, "total_cases": None,
    "rule_count": None, "rule_ids": None, "variant_ids": None, "sampling_seed": 20261117,
    "rules_decl": None, "case_texts_dir": "../00 - event_log/data_text",
    "repetitions": 1, "conditions": ["A"], "evidence_jsonl": None,
    "max_output_tokens": 16000, "timeout_seconds": 300,
    "assessment_mode": "rule", "request_workers": 3, "requests_per_provider": 1,
    "case_protocol": "requirement_declare_v1",
    "max_document_characters": 24000, "max_case_characters": 120000,
    "recovery_max_output_tokens": 32000,
    "article_report_view": "first_and_latest",
    "baseline_phase1_dir": None, "ocr_sidecars_dir": None,
    "cohort_source_dir": None,
    "deduplicate_exact_text": False, "exclude_identifier_only": False,
}
REQUIRED = {"rules_csv", "event_log_csv", "providers"}
SUMMARY_FILES = {
    "model_summary": "summary_by_model.csv", "rule_summary": "summary_by_rule.csv",
    "activation_summary": "summary_by_activation.csv", "confusion_matrix": "confusion_matrix.csv",
    "usage_summary": "usage_by_model.csv",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}.")
        result[key] = value
    return result


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=_unique_object)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open(encoding="utf-8-sig") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            row = json.loads(line, object_pairs_hook=_unique_object)
            if not isinstance(row, dict):
                raise ValueError(f"{path.name}, line {number}: expected a JSON object.")
            rows.append(row)
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(canonical(row) + "\n")


def write_table(path: Path, rows: list[dict[str, Any]], columns: list[str] | None = None) -> None:
    """Serialise nested fields as JSON, leaving unavailable values empty."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if columns is None:
        columns = list(dict.fromkeys(key for row in rows for key in row))
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="raise")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: canonical(value) if isinstance(value, (dict, list)) else value
                             for key, value in row.items()})
    temporary.replace(path)


def load_config(path: str | Path) -> tuple[dict[str, Any], Path]:
    path = Path(path).resolve()
    supplied = read_json(path)
    if not isinstance(supplied, dict):
        raise ValueError("The configuration must be a JSON object.")
    if "requirements_per_case" in supplied:
        raise ValueError("Replace requirements_per_case with rule_count. The selected rules are now shared by every selected case; use a new output_dir.")
    if "case_count" in supplied:
        raise ValueError("Replace case_count with cases_per_year and set year_start and year_end. Cases are now sampled separately for each year; use a new output_dir.")
    unknown = set(supplied) - set(DEFAULTS) - REQUIRED
    if unknown:
        raise ValueError("Unknown configuration fields: " + ", ".join(sorted(unknown)))
    if missing := REQUIRED - set(supplied):
        raise ValueError("Missing configuration fields: " + ", ".join(sorted(missing)))
    config = {**DEFAULTS, **supplied}
    if config["assessment_mode"] not in {"rule", "case"}:
        raise ValueError("assessment_mode must be rule or case.")
    if not isinstance(config["case_protocol"], str) or config["case_protocol"] not in {"requirement_declare_v1", "critical_v1"}:
        raise ValueError("case_protocol must be requirement_declare_v1 or critical_v1.")
    if config["case_protocol"] == "critical_v1" and config["assessment_mode"] != "case":
        raise ValueError("critical_v1 requires assessment_mode case.")
    if config["article_report_view"] not in {"latest", "first_and_latest"}:
        raise ValueError("article_report_view must be latest or first_and_latest.")
    for name in ("request_workers", "requests_per_provider", "recovery_max_output_tokens"):
        value = config[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be a positive integer.")
    for name in ("max_document_characters", "max_case_characters"):
        value = config[name]
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
            raise ValueError(f"{name} must be a positive integer or null for full text.")
    for name in ("deduplicate_exact_text", "exclude_identifier_only"):
        if not isinstance(config[name], bool):
            raise ValueError(f"{name} must be true or false.")
    if config["baseline_phase1_dir"] is not None and (config["assessment_mode"] != "case" or config["conditions"] != ["B"]):
        raise ValueError("baseline_phase1_dir requires case assessment and conditions [B].")
    if config["cohort_source_dir"] is not None and (config["assessment_mode"] != "case"
                                                     or config["conditions"] not in (["A"], ["B"])):
        raise ValueError("cohort_source_dir requires a single case-assessment phase A or B.")
    if config["case_texts_dir"] is None:
        raise ValueError("case_texts_dir must be a non-empty directory path; every selected case requires a document.")
    validate_year_range(config["year_start"], config["year_end"])
    min_events = config["min_events"]
    if (isinstance(min_events, bool) or not isinstance(min_events, int)
            or (min_events != -1 and min_events < 1)):
        raise ValueError("min_events must be a positive integer or -1 for no minimum.")
    for name in ("repetitions", "max_output_tokens"):
        value = config[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be a positive integer.")
    for name in ("cases_per_year", "rule_count"):
        value = config[name]
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)
                                  or (value != -1 and value < 1)):
            raise ValueError(f"{name} must be a positive integer, -1 or null.")
        if value == -1:
            config[name] = None
    total = config["total_cases"]
    if total is not None:
        if isinstance(total, bool) or not isinstance(total, int) or total < 1:
            raise ValueError("total_cases must be a positive integer or null.")
        if config["cases_per_year"] is None or config["case_ids"] is not None:
            raise ValueError("total_cases requires annual quotas and case_ids set to null.")
    if isinstance(config["sampling_seed"], bool) or not isinstance(config["sampling_seed"], int):
        raise ValueError("sampling_seed must be an integer.")
    if isinstance(config["timeout_seconds"], bool) or not isinstance(config["timeout_seconds"], (int, float)) or config["timeout_seconds"] <= 0:
        raise ValueError("timeout_seconds must be positive.")
    if config["formal_mode"] not in {"import", "compute"}:
        raise ValueError("formal_mode must be import or compute.")
    if config["formal_mode"] == "import" and not config["formal_results"]:
        raise ValueError("Import mode requires formal_results.")
    if config["formal_mode"] == "compute" and config["formal_results"] is not None:
        raise ValueError("Set formal_results to null when computing a new reference.")
    if config["case_category"] is not None and not isinstance(config["case_category"], str):
        raise ValueError("case_category must be a string or null.")
    country = config["country"]
    if country is not None:
        if not isinstance(country, str) or not country.strip():
            raise ValueError("country must be a recorded Country value or null for all countries.")
        config["country"] = None if country.strip().casefold() == "none" else country.strip()
    for name in ("case_ids", "rule_ids", "variant_ids"):
        value = config[name]
        if value is not None and (not isinstance(value, list) or not value or
                                  any(not isinstance(item, str) or not item for item in value) or len(value) != len(set(value))):
            raise ValueError(f"{name} must be a non-empty list of unique identifiers or null.")
    conditions = config["conditions"]
    if not isinstance(conditions, list) or not conditions or any(item not in {"A", "B", "C"} for item in conditions) or len(conditions) != len(set(conditions)):
        raise ValueError("conditions must be a non-empty list drawn from A, B and C without duplicates.")
    if config["assessment_mode"] == "case" and ("C" in conditions or config["evidence_jsonl"] is not None):
        raise ValueError("Case assessment supports phases A and B with texts from case_texts_dir; set evidence_jsonl to null.")
    if config["assessment_mode"] == "rule" and any(item in {"B", "C"} for item in conditions) and not config["evidence_jsonl"]:
        raise ValueError("Conditions B and C require evidence_jsonl containing frozen retrieved passages.")
    providers = config["providers"]
    if not isinstance(providers, list) or not providers:
        raise ValueError("Configure at least one provider.")
    ids = []
    for provider in providers:
        if not isinstance(provider, dict):
            raise ValueError("Each provider configuration must be an object.")
        identifier = provider.get("id")
        if not isinstance(identifier, str) or not identifier or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for char in identifier):
            raise ValueError("Provider IDs must contain lowercase letters, digits, hyphens or underscores.")
        if set(provider) - {"id", "provider", "model", "api_key_env", "api_version", "reasoning_effort"}:
            raise ValueError("Unknown provider configuration field; credentials belong in environment variables.")
        build_request(provider, "Configuration validation.", "Configuration validation.", config["max_output_tokens"])
        ids.append(identifier)
    if len(ids) != len(set(ids)):
        raise ValueError("Provider IDs must be unique.")
    for name in ("rules_csv", "rules_decl", "event_log_csv", "output_dir", "formal_results", "evidence_jsonl", "case_texts_dir", "baseline_phase1_dir", "ocr_sidecars_dir", "cohort_source_dir"):
        value = config[name]
        if value is not None and (not isinstance(value, str) or not value):
            raise ValueError(f"{name} must be a path string.")
    return config, path.parent


def resolve(root: Path, value: str) -> Path:
    return (root / Path(value).expanduser()).resolve()


def path_digest(path: Path) -> str:
    if path.is_file():
        return sha256_file(path)
    if path.is_dir():
        entries = [(str(file.relative_to(path)), sha256_file(file))
                   for file in sorted(path.rglob("*")) if file.is_file()]
        if not entries:
            raise ValueError(f"Empty input directory: {display_path(path)}.")
        return digest(entries)
    raise ValueError(f"Input does not exist: {display_path(path)}.")


def code_digest() -> str:
    root = Path(__file__).resolve().parent.parent
    paths = [root / "run_assessment.py"]
    for directory in ("curia_assessment", "curia_conformance"):
        paths.extend(sorted((root / directory).glob("*.py")))
        paths.extend(sorted((root / directory).glob("*.mjs")))
    return digest([(str(path.relative_to(root)), sha256_file(path)) for path in paths])


def experiment_identity(config: dict[str, Any], root: Path) -> dict[str, Any]:
    inputs = {}
    for name in ("rules_csv", "rules_decl", "event_log_csv", "formal_results", "evidence_jsonl"):
        if config[name] is not None:
            inputs[name] = path_digest(resolve(root, config[name]))
    # Only document presence determines eligibility; document text is not model input.
    document_index = index_case_documents(resolve(root, config["case_texts_dir"]))
    inputs["case_texts_dir"] = digest(sorted(document_index))
    identity = {"configuration": config, "input_sha256": inputs,
                "code_sha256": code_digest(), "pipeline_version": __version__}
    return {**identity, "experiment_sha256": digest(identity)}


def validate_decl_catalogue(path: Path, variants: list[dict[str, Any]]) -> None:
    """Check the current rule IDs and expressions before joining textual evidence."""
    from curia_conformance.model import load_rules

    declared = {rule.rule_id: rule.constraint for rule in load_rules(path)}
    catalogue = {}
    for variant in variants:
        rule_id = variant["base_rule_id"]
        if rule_id in catalogue and catalogue[rule_id] != variant["declare"]:
            raise ValueError(f"Textual variants of {rule_id} use different DECLARE expressions.")
        catalogue[rule_id] = variant["declare"]
    if set(catalogue) != set(declared):
        raise ValueError("The textual catalogue and DECLARE file must contain exactly the same original rule IDs.")
    for rule_id, expression in catalogue.items():
        if expression.strip() != declared[rule_id].strip():
            raise ValueError(f"The textual catalogue and DECLARE file disagree on {rule_id}; reconcile them explicitly.")


def load_evidence(path: Path | None, pairs: list[dict[str, Any]], required: bool) -> dict[tuple[str, str], list[dict[str, Any]]]:
    if path is None:
        if required:
            raise ValueError("Frozen evidence is required for this condition.")
        return {}
    evidence = {}
    for record in read_jsonl(path):
        if set(record) != {"case_id", "variant_id", "passages"}:
            raise ValueError("Evidence records require exactly case_id, variant_id and passages.")
        if not all(isinstance(record[field], str) and record[field] for field in ("case_id", "variant_id")):
            raise ValueError("Evidence identifiers must be non-empty strings.")
        key = (record["case_id"], record["variant_id"])
        if key in evidence:
            raise ValueError("Duplicate evidence record for a case and textual variant.")
        evidence[key] = record["passages"]
    for pair in pairs:
        key = (pair["case_id"], pair["variant_id"])
        if required and key not in evidence:
            raise ValueError(f"Missing frozen evidence for {key!r}.")
        if key in evidence:
            build_prompts(pair, "B", evidence[key])
    return evidence


def verify_prepared(output: Path, identity: dict[str, Any]) -> dict[str, Any]:
    manifest = read_json(output / "run_manifest.json")
    if manifest.get("experiment_sha256") != identity["experiment_sha256"]:
        differences = []
        for key, label in (("configuration", "configuration fields"), ("input_sha256", "input files")):
            previous, current = manifest.get(key, {}), identity[key]
            changed = sorted(name for name in previous.keys() | current.keys()
                             if name not in previous or name not in current or previous[name] != current[name])
            if changed:
                differences.append(f"{label}: {', '.join(changed)}")
        if manifest.get("code_sha256") != identity["code_sha256"]:
            differences.append("executable code")
        if manifest.get("pipeline_version") != identity["pipeline_version"]:
            differences.append("pipeline version")
        detail = "; ".join(differences) or "experiment identity"
        raise ValueError(
            "Configuration, inputs or code differ from the prepared experiment. "
            f"Changed: {detail}. Output directory: {display_path(output)}. "
            "Set output_dir in your configuration to a new directory, then run prepare again to preserve the original run.")
    for relative, expected in manifest["prepared_sha256"].items():
        path = output / relative
        if not path.is_file() or sha256_file(path) != expected:
            raise ValueError(f"Prepared evidence or plan changed: {relative}.")
    return manifest


@contextmanager
def output_lock(output: Path) -> Iterator[None]:
    """Prevent simultaneous runners; never silently break a possibly live lock."""
    output.mkdir(parents=True, exist_ok=True)
    path = output / ".assessment.lock"
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ValueError("The output directory is locked. If an earlier process terminated, confirm it is no longer running and remove .assessment.lock before resuming.") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(canonical({"process_id": os.getpid(), "created_utc": utc_now()}))
        yield
    finally:
        path.unlink(missing_ok=True)


def prepare(config_path: str | Path) -> dict[str, Any]:
    config, root = load_config(config_path)
    if config["assessment_mode"] == "case":
        from .case_pipeline import prepare_case
        return prepare_case(config_path)
    output = resolve(root, config["output_dir"])
    identity = experiment_identity(config, root)
    with output_lock(output):
        if (output / "run_manifest.json").exists():
            return verify_prepared(output, identity)
        if (output / "requests").exists():
            raise ValueError("Unmanifested requests exist in this output directory; use a new directory.")
        prepared = output / "prepared"
        variants = load_catalogue(resolve(root, config["rules_csv"]))
        if config["rules_decl"] is not None:
            validate_decl_catalogue(resolve(root, config["rules_decl"]), variants)
        selected_rules, rule_audit = select_requirements(
            variants, config["rule_count"], config["rule_ids"], config["variant_ids"], config["sampling_seed"])
        eligible, _, cleaning = load_cases(resolve(root, config["event_log_csv"]),
                                          min_events=config["min_events"], category=config["case_category"],
                                          country=config["country"],
                                          year_start=config["year_start"], year_end=config["year_end"],
                                          case_texts_dir=resolve(root, config["case_texts_dir"]),
                                          output_dir=prepared / "cleaning")
        cases, case_audit = select_cases_by_year(
            eligible, config["year_start"], config["year_end"], config["cases_per_year"],
            config["sampling_seed"], config["case_ids"], total_cases=config["total_cases"])
        pairs = cross_pairs(cases, selected_rules)
        if config["formal_mode"] == "import":
            formal, provenance = import_formal_results(resolve(root, config["formal_results"]), cases, selected_rules,
                                                       resolve(root, config["event_log_csv"]), config["min_events"])
        else:
            formal = compute_formal_results(cases, selected_rules, prepared / "computed_formal")
            provenance = {"mode": "compute", "engine_manifest": "prepared/computed_formal/formal_manifest.json"}
        evidence = load_evidence(resolve(root, config["evidence_jsonl"]) if config["evidence_jsonl"] else None,
                                 pairs, any(condition in {"B", "C"} for condition in config["conditions"]))
        for pair in pairs:
            pair["passages"] = evidence.get((pair["case_id"], pair["variant_id"]), [])
        jobs = []
        for condition in config["conditions"]:
            for repetition in range(1, config["repetitions"] + 1):
                for pair in pairs:
                    system, user, _ = build_prompts(pair, condition, pair["passages"] if condition != "A" else None)
                    for provider in config["providers"]:
                        request = build_request(provider, system, user, config["max_output_tokens"])
                        request_hash = digest(request)
                        job = {
                            "provider_id": provider["id"], "provider": provider["provider"],
                            "model": provider["model"], "condition": condition, "run": repetition,
                            "case_id": pair["case_id"], "base_rule_id": pair["base_rule_id"],
                            "variant_id": pair["variant_id"], "requirement_review_id": pair["requirement_review_id"],
                            "pair_id": pair["pair_id"], "trace_sha256": pair["trace_sha256"],
                            "request_sha256": request_hash,
                            "prompt_sha256": digest([system, user]),
                            "evidence_sha256": digest(pair["passages"] if condition != "A" else []),
                        }
                        job["job_id"] = "job_" + digest(job)[:24]
                        jobs.append(job)
        if len({job["job_id"] for job in jobs}) != len(jobs):
            raise ValueError("Planned job identifiers are not unique.")
        write_table(prepared / "resolved_rules.csv", variants)
        write_table(prepared / "selected_rules.csv", selected_rules)
        write_table(prepared / "rule_selection.csv", rule_audit)
        write_table(prepared / "selected_cases.csv", [{key: value for key, value in case.items() if key != "events"} for case in cases])
        write_table(prepared / "case_sampling_by_year.csv", case_audit)
        write_table(prepared / "selected_pairs.csv", [{key: pair[key] for key in (
            "pair_id", "case_id", "base_rule_id", "variant_id", "requirement_review_id", "event_count", "trace_sha256")}
            for pair in pairs])
        write_jsonl(prepared / "pairs.jsonl", pairs)
        write_jsonl(prepared / "planned_assessments.jsonl", jobs)
        write_jsonl(prepared / "formal_results.jsonl", formal)
        write_table(prepared / "formal_results.csv", formal)
        write_json(prepared / "formal_provenance.json", provenance)
        write_json(prepared / "cleaning" / "selection_summary.json", cleaning)
        first_condition = config["conditions"][0]
        example_system, example_user, _ = build_prompts(pairs[0], first_condition,
                                                       pairs[0]["passages"] if first_condition != "A" else None)
        (prepared / "example_prompt.txt").write_text("SYSTEM MESSAGE\n" + example_system + "\n\nUSER MESSAGE\n" + example_user + "\n", encoding="utf-8")
        extraction_report = write_case_extraction_report(output, config, cases, case_audit, cleaning)
        manifest = {
            **identity, "created_utc": utc_now(), "python_version": platform.python_version(),
            "source_cases": cleaning["source_cases"], "source_events": cleaning["source_events"],
            "excluded_cases": cleaning["excluded_cases"], "eligible_cases": len(eligible),
            "sampled_cases": len(cases), "catalogue_base_rules": len({item["base_rule_id"] for item in variants}),
            "catalogue_textual_variants": len(variants), "selected_pairs": len(pairs),
            "selected_base_rules": len({item["base_rule_id"] for item in pairs}),
            "selected_textual_variants": len({item["variant_id"] for item in pairs}),
            "planned_assessments": len(jobs), "formal_reference_pairs": len(formal),
            "country_filter": config["country"],
            "case_sampling_by_year": case_audit,
            "case_extraction_report": extraction_report.name,
            "selection_method": "Filter whole cases by recorded category, country, event count, CaseID year and at least one non-empty local case document; sample distinct cases per year, then compensate shortages one case per year in descending year order, repeating until the configured total or the eligible pool is exhausted; explicit case_ids override quotas and compensation but satisfy all eligibility filters; cross every selected case with every selected rule for all models; no outcome stratification.",
            "analysis_unit": "Case and textual requirement variant, linked to its base DECLARE rule.",
            "prepared_sha256": {
                **{str(path.relative_to(output)): sha256_file(path)
                   for path in sorted(prepared.rglob("*")) if path.is_file()},
                extraction_report.name: sha256_file(extraction_report),
            },
        }
        write_json(output / "run_manifest.json", manifest)
    analyse(config_path)
    return manifest


def _attempt_directories(output: Path, job: dict[str, Any]) -> list[Path]:
    directory = output / "requests" / job["job_id"]
    if not directory.exists():
        return []
    attempts = sorted(path for path in directory.iterdir() if path.is_dir() and path.name.startswith("attempt_"))
    if any(not path.name[8:].isdigit() for path in attempts):
        raise ValueError("Unexpected request-attempt directory name.")
    return sorted(attempts, key=lambda path: int(path.name[8:]))


def _attempt_result(path: Path, job: dict[str, Any]) -> dict[str, Any]:
    if (path / "request.json").is_file() and digest(read_json(path / "request.json")) != job["request_sha256"]:
        raise ValueError("An archived API request differs from its prepared request hash.")
    if (path / "assessment.json").exists():
        result = read_json(path / "assessment.json")
        if result.get("job_id") != job["job_id"] or result.get("request_sha256") != job["request_sha256"]:
            raise ValueError("An archived assessment does not match its planned job.")
        response_path = path / "response.json"
        if not (path / "request.json").is_file():
            raise ValueError("An archived assessment has no archived request.")
        if not response_path.is_file() or sha256_file(response_path) != result.get("raw_response_sha256"):
            raise ValueError("An archived provider response changed or is missing.")
        return result
    return {
        "job_id": job["job_id"], "request_sha256": job["request_sha256"],
        "status": "interrupted", "outcome": None, "justification": None, "evidence_refs": [],
        "error": {"code": "interrupted_attempt", "message": "Attempt has no final assessment; transmission or completion is unknown. It is not retried automatically."},
    }


def _analyse_prepared(output: Path) -> dict[str, list[dict[str, Any]]]:
    jobs = read_jsonl(output / "prepared" / "planned_assessments.jsonl")
    pairs = {pair["pair_id"]: pair for pair in read_jsonl(output / "prepared" / "pairs.jsonl")}
    write_event_log_statistics(output, pairs.values())
    predictions, ledger = [], []
    for job in jobs:
        directories = _attempt_directories(output, job)
        attempts = []
        for path in directories:
            archived = _attempt_result(path, job)
            attempts.append({**archived, **normalise_usage(job["provider"], archived.get("usage"))})
        for index, (directory, result) in enumerate(zip(directories, attempts, strict=True), 1):
            ledger.append({**job, "attempt": int(directory.name[8:]), "is_latest_attempt": index == len(attempts),
                           "attempt_directory": str(directory.relative_to(output)), **result})
        result = attempts[-1] if attempts else {
            "status": "pending", "outcome": None, "justification": None, "evidence_refs": [], "call_attempted": False,
            "usage": None, "elapsed_seconds": None, **normalise_usage(job["provider"], None),
        }
        pair = pairs[job["pair_id"]]
        predictions.append({**job, "attempt_count": len(attempts),
                            "selected_attempt": int(directories[-1].name[8:]) if directories else None,
                            "requirement": pair["requirement"], "declare": pair["declare"], **result})
    formal = read_jsonl(output / "prepared" / "formal_results.jsonl")
    comparisons = compare_rows(predictions, formal)
    summaries = summarise_comparisons(comparisons)
    usage_summary = summarise_resource_usage(jobs, ledger)
    group_fields = ("provider", "model", "condition", "run")
    usage_by_group = {tuple(row[field] for field in group_fields): row for row in usage_summary}
    for row in summaries["model_summary"]:
        row.update(usage_by_group[tuple(row[field] for field in group_fields)])
    summaries["usage_summary"] = usage_summary
    write_table(output / "llm_assessments.csv", predictions)
    write_table(output / "attempts.csv", ledger, None if ledger else [
        "job_id", "attempt", "status", "call_attempted", "elapsed_seconds", "usage", *TOKEN_FIELDS,
    ])
    write_table(output / "comparison_by_pair.csv", comparisons)
    for key, filename in SUMMARY_FILES.items():
        write_table(output / filename, summaries[key])
    write_json(output / "analysis_manifest.json", {
        "created_utc": utc_now(), "planned_assessments": len(jobs), "recorded_attempts": len(ledger),
        "selection_policy": "Latest recorded attempt, including a failed or interrupted latest attempt; earlier attempts remain in the ledger.",
        "resource_usage_policy": "All recorded attempts, including invalid, failed and retried attempts; API time sums reported request durations and token totals sum known normalised usage with per-metric coverage counts. Missing values are not imputed.",
        "resource_summary_file": "usage_by_model.csv",
        "output_status_counts": dict(Counter(row["status"] for row in predictions)),
        "interpretation": "Agreement with a formal trace evaluation; not an estimate of legal accuracy.",
    })
    return summaries


def analyse(config_path: str | Path) -> dict[str, list[dict[str, Any]]]:
    config, root = load_config(config_path)
    if config["assessment_mode"] == "case":
        from .case_pipeline import analyse_case
        return analyse_case(config_path)
    output = resolve(root, config["output_dir"])
    with output_lock(output):
        verify_prepared(output, experiment_identity(config, root))
        return _analyse_prepared(output)


def run(config_path: str | Path, provider_id: str | None = None,
        max_requests: int | None = None, retry_failed: bool = False) -> dict[str, int]:
    """Make one explicitly recorded attempt per job; never choose a better retry."""
    config, root = load_config(config_path)
    if config["assessment_mode"] == "case":
        from .case_pipeline import run_case
        return run_case(config_path, provider_id, max_requests, retry_failed)
    if provider_id is not None and provider_id not in {item["id"] for item in config["providers"]}:
        raise ValueError(f"Provider ID is not configured: {provider_id}.")
    if max_requests is not None and (isinstance(max_requests, bool) or not isinstance(max_requests, int) or max_requests < 1):
        raise ValueError("max_requests must be a positive integer.")
    output = resolve(root, config["output_dir"])
    if not (output / "run_manifest.json").exists():
        prepare(config_path)
    counts = {"processed": 0, "successful": 0, "failed": 0, "skipped_existing": 0, "blocked_providers": 0}
    with output_lock(output):
        verify_prepared(output, experiment_identity(config, root))
        jobs = read_jsonl(output / "prepared" / "planned_assessments.jsonl")
        pairs = {pair["pair_id"]: pair for pair in read_jsonl(output / "prepared" / "pairs.jsonl")}
        providers = {item["id"]: item for item in config["providers"]}
        blocked = set()
        try:
            for job in jobs:
                if provider_id is not None and job["provider_id"] != provider_id:
                    continue
                if job["provider_id"] in blocked:
                    continue
                if max_requests is not None and counts["processed"] >= max_requests:
                    break
                directories = _attempt_directories(output, job)
                if directories:
                    latest = _attempt_result(directories[-1], job)
                    if not retry_failed or latest["status"] == "ok":
                        counts["skipped_existing"] += 1
                        continue
                pair = pairs[job["pair_id"]]
                system, user, refs = build_prompts(pair, job["condition"], pair["passages"] if job["condition"] != "A" else None)
                request = build_request(providers[job["provider_id"]], system, user, config["max_output_tokens"])
                if digest(request) != job["request_sha256"]:
                    raise ValueError("Generated API request differs from its prepared hash.")
                number = int(directories[-1].name[8:]) + 1 if directories else 1
                directory = output / "requests" / job["job_id"] / f"attempt_{number:03d}"
                directory.mkdir(parents=True, exist_ok=False)
                write_json(directory / "request.json", request)
                write_json(directory / "started.json", {"job_id": job["job_id"], "created_utc": utc_now(),
                                                        "attempt": number, "explicit_retry": bool(directories)})
                print(f"{job['provider_id']} | run {job['run']} | {job['case_id']} | {job['variant_id']} | attempt {number}", flush=True)
                response = send_request(request, timeout_seconds=config["timeout_seconds"])
                write_json(directory / "response.json", response)
                if response["status"] == "ok":
                    result = parse_assessment(response.get("text", ""), refs)
                else:
                    response_error = response.get("error")
                    output_status = ("no_text" if response["status"] == "error" and isinstance(response_error, dict)
                                     and response_error.get("code") == "no_text" else response["status"])
                    result = {"status": output_status, "outcome": None, "justification": None,
                              "evidence_refs": [], "error": response.get("error"), "format_notes": []}
                result.update({
                    "job_id": job["job_id"], "request_sha256": job["request_sha256"],
                    "completed_utc": utc_now(), "call_attempted": response.get("attempted"),
                    "model_returned": response.get("model_returned"), "response_id": response.get("response_id"),
                    "provider_request_id": response.get("request_id"), "usage": response.get("usage"),
                    "http_status": response.get("http_status"), "finish_reason": response.get("finish_reason"),
                    "elapsed_seconds": response.get("elapsed_seconds"),
                    "raw_response_sha256": sha256_file(directory / "response.json"),
                })
                result.update(normalise_usage(job["provider"], response.get("usage")))
                write_json(directory / "assessment.json", result)
                counts["processed"] += 1
                counts["successful" if result["status"] == "ok" else "failed"] += 1
                print(f"  Outcome: {result.get('outcome') or result['status']}", flush=True)
                if result["status"] != "ok":
                    detail = format_error(response) if response["status"] != "ok" else canonical(result.get("error"))
                    print(f"  Details: {detail}", flush=True)
                if response.get("http_status") in {400, 401, 403, 404, 429} or response.get("attempted") is False:
                    blocked.add(job["provider_id"])
                    print("  Further requests to this provider are paused for this invocation; other providers can continue.", flush=True)
        finally:
            counts["blocked_providers"] = len(blocked)
            _analyse_prepared(output)
    return counts
