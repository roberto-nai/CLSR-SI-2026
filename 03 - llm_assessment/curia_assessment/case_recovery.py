"""Recover length-truncated phase-A calls through a frozen request allowlist."""

from __future__ import annotations

import copy
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import pipeline as p
from .case_prompts import build_case_prompts, parse_case_assessment

_PLAN_FILE = "recovery_plan.json"
_INITIAL_LIMIT = 16000
_RECOVERY_LIMIT = 32000
_ATTEMPT = re.compile(r"attempt_([0-9]+)")


def _job_directory(audit: Path, job: Mapping[str, Any]) -> Path:
    identifier = job.get("job_id")
    if (not isinstance(identifier, str) or not identifier or identifier in {".", ".."}
            or Path(identifier).name != identifier):
        raise ValueError("Recovery requires an ordinary job identifier, not a path.")
    return audit / "requests" / identifier


def _attempt_number(directory: Path) -> int:
    match = _ATTEMPT.fullmatch(directory.name)
    if match is None or int(match.group(1)) < 1:
        raise ValueError("Recovery requires a numbered attempt directory.")
    return int(match.group(1))


def _token_field(request: Mapping[str, Any]) -> str:
    provider = request.get("provider")
    if provider == "openai":
        return "max_output_tokens"
    if provider in {"anthropic", "mistral"}:
        return "max_tokens"
    raise ValueError("Unknown provider in an archived recovery request.")


def _limit(request: Mapping[str, Any]) -> int:
    field = _token_field(request)
    value = request.get("payload", {}).get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("An archived request has no valid output-token limit.")
    return value


def _initial_request(audit: Path, job: Mapping[str, Any]) -> dict[str, Any]:
    path = _job_directory(audit, job) / "attempt_001/request.json"
    if not path.is_file():
        raise ValueError("Recovery requires the original archived request.")
    request = p.read_json(path)
    if p.digest(request) != job["request_sha256"]:
        raise ValueError("The original request differs from its frozen request hash.")
    if _limit(request) != _INITIAL_LIMIT:
        raise ValueError("This recovery protocol requires an original 16000-token limit.")
    if any(field in request for field in ("api_key", "headers", "authorization")):
        raise ValueError("Recovery plans must not contain credentials or authorisation headers.")
    return request


def _read_plan(audit: Path, *, require_frozen: bool) -> dict[str, Any]:
    path = audit / _PLAN_FILE
    if not path.is_file():
        raise ValueError("The amended request is not authorised by a recovery plan.")
    if require_frozen:
        manifest_path = audit / "run_manifest.json"
        if not manifest_path.is_file():
            raise ValueError("The recovery plan has not been frozen in the run manifest.")
        expected = p.read_json(manifest_path).get("prepared_sha256", {}).get(_PLAN_FILE)
        if not expected or p.sha256_file(path) != expected:
            raise ValueError("The recovery plan differs from its frozen manifest hash.")
    plan = p.read_json(path)
    if not isinstance(plan, dict) or not isinstance(plan.get("entries"), list):
        raise ValueError("The recovery plan requires an entries list.")
    seen = set()
    for entry in plan["entries"]:
        if not isinstance(entry, dict):
            raise ValueError("Each recovery authorisation must be an object.")
        identifier, attempt = entry.get("job_id"), entry.get("attempt")
        if (not isinstance(identifier, str) or not identifier or isinstance(attempt, bool)
                or not isinstance(attempt, int) or attempt < 2):
            raise ValueError("A recovery authorisation requires a job_id and a later attempt number.")
        key = (identifier, attempt)
        if key in seen:
            raise ValueError("Duplicate job/attempt authorisation in recovery plan.")
        seen.add(key)
    return plan


def _validate_entry(audit: Path, job: dict[str, Any], entry: Mapping[str, Any]) -> dict[str, Any]:
    """Allow exactly the authorised token-cap increase, not another request."""
    if job.get("condition") != "A" or entry.get("job_id") != job["job_id"]:
        raise ValueError("Only the named phase-A job may use a recovery authorisation.")
    if entry.get("original_request_sha256") != job["request_sha256"]:
        raise ValueError("Recovery authorisation does not match the original request hash.")
    attempt, previous = entry.get("attempt"), entry.get("previous_attempt")
    if (isinstance(attempt, bool) or not isinstance(attempt, int)
            or isinstance(previous, bool) or not isinstance(previous, int)
            or previous < 1 or attempt != previous + 1):
        raise ValueError("Recovery must be the next attempt after its recorded predecessor.")
    if entry.get("max_output_tokens") != _RECOVERY_LIMIT or isinstance(entry.get("max_output_tokens"), bool):
        raise ValueError("The only authorised recovery output-token limit is 32000.")
    if entry.get("previous_finish_reason") != "length":
        raise ValueError("Recovery is authorised only after a length-truncated response.")
    baseline = _initial_request(audit, job)
    previous_directory = _job_directory(audit, job) / f"attempt_{previous:03d}"
    previous_result = p._attempt_result(previous_directory, job)
    response_path = previous_directory / "response.json"
    if (previous_result.get("status") != "incomplete" or not response_path.is_file()
            or p.read_json(response_path).get("finish_reason") != "length"):
        raise ValueError("Recovery predecessor must be an archived incomplete response with finish_reason=length.")
    expected = copy.deepcopy(baseline)
    expected["payload"][_token_field(expected)] = _RECOVERY_LIMIT
    request = entry.get("request")
    if request != expected or entry.get("request_sha256") != p.digest(expected):
        raise ValueError("The recovery request changes more than the authorised output-token limit.")
    return expected


def read_case_attempt(audit: Path, job: dict[str, Any], directory: Path) -> dict[str, Any]:
    """Read a baseline attempt or an exact, frozen, per-attempt amendment."""
    audit, directory = Path(audit), Path(directory)
    number = _attempt_number(directory)
    if directory.resolve().parent != _job_directory(audit, job).resolve():
        raise ValueError("Attempt directory does not belong to this case job.")
    request_path = directory / "request.json"
    if not request_path.is_file():
        result = p._attempt_result(directory, job)
        return {**result, "actual_output_token_limit": None}
    request = p.read_json(request_path)
    actual_hash = p.digest(request)
    verified_job = job
    if actual_hash != job["request_sha256"]:
        plan = _read_plan(audit, require_frozen=True)
        matches = [entry for entry in plan["entries"] if entry["job_id"] == job["job_id"] and entry["attempt"] == number]
        if len(matches) != 1:
            raise ValueError("This job/attempt request is not authorised by the recovery plan.")
        entry = matches[0]
        expected = _validate_entry(audit, job, entry)
        if actual_hash != entry["request_sha256"] or request != expected:
            raise ValueError("Archived request differs from its authorised recovery request.")
        verified_job = {**job, "request_sha256": actual_hash}
    result = p._attempt_result(directory, verified_job)
    return {**result, "actual_output_token_limit": _limit(request)}


def build_recovery_plan(
    audit: Path, jobs: Iterable[dict[str, Any]], pairs: Mapping[str, dict[str, Any]] | Iterable[dict[str, Any]],
    providers: Mapping[str, dict[str, Any]] | Iterable[dict[str, Any]], max_output_tokens: int = _RECOVERY_LIMIT,
) -> dict[str, Any]:
    """Return, without writing, authorisations for known 16000-token truncations."""
    if isinstance(max_output_tokens, bool) or max_output_tokens != _RECOVERY_LIMIT:
        raise ValueError("This protocol authorises exactly 32000 output tokens for recovery.")
    audit = Path(audit)
    pair_index = dict(pairs) if isinstance(pairs, Mapping) else {row["pair_id"]: row for row in pairs}
    provider_index = dict(providers) if isinstance(providers, Mapping) else {row["id"]: row for row in providers}
    entries = []
    seen = set()
    for job in jobs:
        if job["job_id"] in seen:
            raise ValueError("Recovery planning requires unique case job identifiers.")
        seen.add(job["job_id"])
        if job.get("condition") != "A":
            continue
        directories = p._attempt_directories(audit, job)
        if not directories:
            continue
        previous_directory = directories[-1]
        latest = read_case_attempt(audit, job, previous_directory)
        response_path = previous_directory / "response.json"
        if (latest.get("status") != "incomplete" or latest.get("actual_output_token_limit") != _INITIAL_LIMIT
                or not response_path.is_file() or p.read_json(response_path).get("finish_reason") != "length"):
            continue
        baseline = _initial_request(audit, job)
        members = [pair_index[pair_id] for pair_id in job["pair_ids"]]
        system, user, _ = build_case_prompts(members, "A", None,
                                            case_protocol=job.get("case_protocol", "requirement_declare_v1"))
        regenerated = p.build_request(provider_index[job["provider_id"]], system, user, _INITIAL_LIMIT)
        if regenerated != baseline or p.digest(regenerated) != job["request_sha256"]:
            raise ValueError("Regenerated baseline request differs from the original frozen request.")
        request = p.build_request(provider_index[job["provider_id"]], system, user, _RECOVERY_LIMIT)
        previous = _attempt_number(previous_directory)
        entry = {
            "job_id": job["job_id"], "attempt": previous + 1,
            "original_request_sha256": job["request_sha256"], "request_sha256": p.digest(request),
            "max_output_tokens": _RECOVERY_LIMIT, "previous_attempt": previous,
            "previous_finish_reason": "length", "request": request,
        }
        _validate_entry(audit, job, entry)
        entries.append(entry)
    return {
        "created_utc": p.utc_now(), "conditions": ["A"], "initial_output_token_limit": _INITIAL_LIMIT,
        "recovery_output_token_limit": _RECOVERY_LIMIT, "entries": entries,
        "policy": "One explicit recovery attempt for a known length-truncated 16000-token phase-A request; only the output-token limit changes to 32000. Latest attempts determine results and every attempt remains in resource accounting.",
    }


def _freeze_plan_if_needed(
    audit: Path, config: Mapping[str, Any], jobs: Mapping[str, dict[str, Any]], pairs: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    """Record a configured recovery amendment without changing baseline identity."""
    path = audit / _PLAN_FILE
    previous_plan = _read_plan(audit, require_frozen=False) if path.exists() else None
    if previous_plan is not None and previous_plan["entries"]:
        return _read_plan(audit, require_frozen=True)
    plan = build_recovery_plan(
        audit, jobs.values(), pairs, config["providers"],
        max_output_tokens=config.get("recovery_max_output_tokens", _RECOVERY_LIMIT),
    )
    manifest_path = audit / "run_manifest.json"
    manifest = p.read_json(manifest_path)
    if previous_plan is not None and not plan["entries"]:
        # Repeated invocations with no eligible cases do not add empty amendments.
        if manifest.get("prepared_sha256", {}).get(_PLAN_FILE) == p.sha256_file(path):
            return previous_plan
    stamp = re.sub(r"[^0-9]", "", p.utc_now())
    archive = audit / "continuations" / ("recovery_plan_" + stamp)
    archive.mkdir(parents=True, exist_ok=False)
    before_manifest = manifest_path.read_bytes()
    (archive / "run_manifest.json").write_bytes(before_manifest)
    old_plan_hash = None
    if path.exists():
        (archive / _PLAN_FILE).write_bytes(path.read_bytes())
        old_plan_hash = p.sha256_file(path)
    p.write_json(path, plan)
    provenance = {
        "created_utc": p.utc_now(), "reason": "Configured recovery of known length-truncated phase-A calls.",
        "experiment_sha256": manifest.get("experiment_sha256"),
        "previous_manifest_sha256": p.sha256_file(archive / "run_manifest.json"),
        "previous_recovery_plan_sha256": old_plan_hash, "recovery_plan_sha256": p.sha256_file(path),
        "initial_output_token_limit": _INITIAL_LIMIT, "recovery_output_token_limit": _RECOVERY_LIMIT,
        "authorised_job_ids": [entry["job_id"] for entry in plan["entries"]],
        "identity_policy": "Baseline configuration, inputs, code identity, prepared jobs and archived attempts remain unchanged; the frozen per-attempt allowlist records the token-cap amendment.",
    }
    p.write_json(archive / "provenance.json", provenance)
    updated = copy.deepcopy(manifest)
    updated.setdefault("prepared_sha256", {})[_PLAN_FILE] = p.sha256_file(path)
    updated.setdefault("recovery_provenance", []).append(str((archive / "provenance.json").relative_to(audit)))
    p.write_json(manifest_path, updated)
    return _read_plan(audit, require_frozen=True)


def run_recovery(config_path: str | Path) -> dict[str, int]:
    """Execute each frozen recovery entry at most once, retaining all archives."""
    # case_pipeline imports read_case_attempt; defer the reverse import.
    from . import case_pipeline

    config, root = p.load_config(config_path)
    if config["conditions"] != ["A"]:
        raise ValueError("Recovery requires an A-only frozen experiment.")
    output, audit = case_pipeline._locations(config, root)
    counts = {"planned_recoveries": 0, "processed": 0, "successful": 0, "failed": 0,
              "skipped_existing": 0, "skipped_interrupted": 0}
    count_lock = threading.Lock()
    stop = threading.Event()
    with p.output_lock(audit):
        case_pipeline._verify(config, root, audit)
        jobs = {job["job_id"]: job for job in p.read_jsonl(audit / "prepared/planned_assessments.jsonl")}
        pairs = {pair["pair_id"]: pair for pair in p.read_jsonl(audit / "prepared/pairs.jsonl")}
        plan = _freeze_plan_if_needed(audit, config, jobs, pairs)
        entries = plan["entries"]
        counts["planned_recoveries"] = len(entries)
        # Validate the entire plan before transmitting anything.
        for entry in entries:
            if entry["job_id"] not in jobs:
                raise ValueError("Recovery plan names a job outside the frozen phase-A plan.")
            _validate_entry(audit, jobs[entry["job_id"]], entry)

        def process(entry: dict[str, Any]) -> None:
            job = jobs[entry["job_id"]]
            directory = _job_directory(audit, job) / f"attempt_{entry['attempt']:03d}"
            if directory.exists():
                result = read_case_attempt(audit, job, directory)
                with count_lock:
                    counts["skipped_existing"] += 1
                    counts["skipped_interrupted"] += result.get("status") == "interrupted"
                return
            if stop.is_set():
                return
            members = [pairs[pair_id] for pair_id in job["pair_ids"]]
            _, _, refs = build_case_prompts(members, "A", None,
                                           case_protocol=job.get("case_protocol", "requirement_declare_v1"))
            directory.mkdir(parents=True, exist_ok=False)
            p.write_json(directory / "request.json", entry["request"])
            p.write_json(directory / "started.json", {
                "job_id": job["job_id"], "created_utc": p.utc_now(), "attempt": entry["attempt"],
                "explicit_retry": True, "recovery_authorisation": _PLAN_FILE,
                "original_request_sha256": job["request_sha256"], "actual_output_token_limit": _RECOVERY_LIMIT,
            })
            print(f"{job['provider_id']} | phase A | {job['case_id']} | explicit recovery | 32000 output tokens", flush=True)
            response = p.send_request(entry["request"], timeout_seconds=config["timeout_seconds"])
            p.write_json(directory / "response.json", response)
            if response["status"] == "ok":
                result = parse_case_assessment(response.get("text", ""), job["rule_ids"], refs,
                                               case_protocol=job.get("case_protocol", "requirement_declare_v1"))
            else:
                result = {"status": response["status"], "assessments": [], "error": response.get("error")}
            result.update({
                "job_id": job["job_id"], "request_sha256": entry["request_sha256"],
                "original_request_sha256": job["request_sha256"], "actual_output_token_limit": _RECOVERY_LIMIT,
                "recovery_authorisation": _PLAN_FILE, "completed_utc": p.utc_now(),
                "call_attempted": response.get("attempted"), "model_returned": response.get("model_returned"),
                "response_id": response.get("response_id"), "provider_request_id": response.get("request_id"),
                "usage": response.get("usage"), "elapsed_seconds": response.get("elapsed_seconds"),
                "http_status": response.get("http_status"), "finish_reason": response.get("finish_reason"),
                "raw_response_sha256": p.sha256_file(directory / "response.json"),
            })
            result.update(p.normalise_usage(job["provider"], response.get("usage")))
            p.write_json(directory / "assessment.json", result)
            with count_lock:
                counts["processed"] += 1
                counts["successful" if result["status"] == "ok" else "failed"] += 1
                if response.get("http_status") in {400, 401, 403, 404, 429} or response.get("attempted") is False:
                    stop.set()

        def guarded(entry: dict[str, Any]) -> None:
            try:
                process(entry)
            except BaseException:
                stop.set()
                raise

        try:
            workers = max(1, min(2, config.get("request_workers", 1), config.get("requests_per_provider", 1), len(entries)))
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [executor.submit(guarded, entry) for entry in entries]
                for future in futures:
                    future.result()
        finally:
            case_pipeline._analyse(output, audit, config)
    return counts
