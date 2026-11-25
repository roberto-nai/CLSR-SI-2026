"""Reconstruct a missing assessment from an already saved case API response.

This operation is local: it never sends or retries a request. The caller must
hold the experiment output lock. Only the exact frozen baseline request is
supported here; a changed request needs its separate amendment verification.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
from pathlib import Path
import re
from typing import Any

from . import pipeline as p
from .case_prompts import build_case_prompts, parse_case_assessment
from .metrics import normalise_usage


def _object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = p.read_json(path)
    except (OSError, ValueError, RecursionError) as exc:
        raise ValueError(f"Cannot read the archived {label}: {path}.") from exc
    if not isinstance(value, dict):
        raise ValueError(f"The archived {label} must be a JSON object.")
    try:
        p.digest(value)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError(f"The archived {label} contains unsupported or non-finite JSON values.") from exc
    return value


def _timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty, timezone-aware ISO timestamp.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("Missing timestamp offset.")
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"{label} must be a timezone-aware ISO timestamp.") from exc


def _completion_time(response: dict, started: dict, recovered_utc: str) -> tuple[str, str]:
    received = response.get("received_utc")
    created = started.get("created_utc")
    if received is not None:
        _timestamp(received, "response.received_utc")
    if created is not None:
        _timestamp(created, "started.created_utc")
    elapsed = response.get("elapsed_seconds")
    if elapsed is not None:
        try:
            valid_elapsed = (not isinstance(elapsed, bool) and isinstance(elapsed, (int, float))
                             and math.isfinite(elapsed) and elapsed >= 0)
        except OverflowError:
            valid_elapsed = False
        if not valid_elapsed:
            raise ValueError("response.elapsed_seconds must be a finite, non-negative number or null.")
    if received is not None:
        return received, "response.received_utc"
    if created is not None and elapsed is not None:
        try:
            completed = _timestamp(created, "started.created_utc") + timedelta(seconds=elapsed)
        except (OverflowError, TypeError) as exc:
            raise ValueError("Archived timing cannot produce a valid estimated completion timestamp.") from exc
        return completed.isoformat(), "estimated_started_created_utc_plus_elapsed_seconds"
    return recovered_utc, "local_recovery_time_not_remote_completion"


def _check_response_types(response: dict[str, Any]) -> None:
    status = response.get("status")
    if not isinstance(status, str) or not status or status != status.strip():
        raise ValueError("The archived response status must be a non-empty string.")
    if (status == "ok" and not isinstance(response.get("text"), str)
            or "text" in response and not isinstance(response["text"], str)):
        raise ValueError("The archived response text must be a string.")
    if response.get("attempted") is not None and not isinstance(response["attempted"], bool):
        raise ValueError("The archived response attempted flag must be Boolean or null.")
    if response.get("usage") is not None and not isinstance(response["usage"], dict):
        raise ValueError("The archived response usage must be an object or null.")
    if response.get("error") is not None and not isinstance(response["error"], (dict, str)):
        raise ValueError("The archived response error must be an object, string or null.")
    http = response.get("http_status")
    if http is not None and (isinstance(http, bool) or not isinstance(http, int) or not 100 <= http <= 599):
        raise ValueError("The archived HTTP status must be an integer from 100 to 599 or null.")
    for name in ("model_returned", "response_id", "request_id", "finish_reason"):
        if response.get(name) is not None and not isinstance(response[name], str):
            raise ValueError(f"The archived response {name} must be a string or null.")


def _check_request_prompts(request: dict, system: str, user: str) -> None:
    payload = request.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("The archived request payload must be an object.")
    provider = request.get("provider")
    if provider == "openai":
        matches = payload.get("instructions") == system and payload.get("input") == [
            {"role": "user", "content": [{"type": "input_text", "text": user}]}]
    elif provider == "anthropic":
        matches = payload.get("system") == system and payload.get("messages") == [
            {"role": "user", "content": [{"type": "text", "text": user}]}]
    elif provider == "mistral":
        matches = payload.get("messages") == [
            {"role": "system", "content": system}, {"role": "user", "content": user}]
    else:
        raise ValueError("Local case replay requires a supported archived provider.")
    if not matches:
        raise ValueError("The archived request does not match the supplied frozen case evidence.")


def recover_case_assessment(
    audit: str | Path, job: dict[str, Any], directory: str | Path, pairs: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Write a missing assessment in the same attempt, preserving all source files.

    Existing assessments and attempts without response.json are left untouched.
    A started-only attempt remains ambiguous; this function never authorises its
    retransmission. Non-JSON model text and provider errors retain their original
    operational status through the ordinary parser and are never legal repairs.
    """
    try:
        audit, directory = Path(audit).resolve(), Path(directory).resolve()
    except (TypeError, OSError, RuntimeError) as exc:
        raise ValueError("Local case replay requires valid audit and attempt paths.") from exc
    assessment_path, response_path = directory / "assessment.json", directory / "response.json"
    if assessment_path.exists():
        return None
    if not response_path.is_file():
        return None
    if not isinstance(job, dict):
        raise ValueError("Local case replay requires a frozen job object.")
    identifier = job.get("job_id")
    if (not isinstance(identifier, str) or not identifier or identifier in {".", ".."}
            or Path(identifier).name != identifier):
        raise ValueError("Local case replay requires an ordinary job identifier.")
    match = re.fullmatch(r"attempt_([0-9]+)", directory.name)
    if (not directory.is_relative_to(audit) or directory.parent != (audit / "requests" / identifier).resolve()
            or match is None or int(match.group(1)) < 1):
        raise ValueError("The attempt directory does not belong to this frozen case job.")
    request_path, started_path = directory / "request.json", directory / "started.json"
    if not request_path.is_file() or not started_path.is_file():
        raise ValueError("A saved response requires its archived request.json and started.json before local replay.")
    source_hashes = {path: p.sha256_file(path) for path in (request_path, started_path, response_path)}
    request, started, response = (_object(path, label) for path, label in (
        (request_path, "request"), (started_path, "start record"), (response_path, "response")))
    if not isinstance(job.get("request_sha256"), str) or p.digest(request) != job["request_sha256"]:
        raise ValueError("Local replay supports only the exact frozen baseline request hash; amended requests require separate authorisation.")
    if started.get("job_id") != identifier:
        raise ValueError("The archived start record names a different case job.")
    attempt = started.get("attempt")
    if attempt is not None and (isinstance(attempt, bool) or not isinstance(attempt, int) or attempt != int(match.group(1))):
        raise ValueError("The archived start record has a different or invalid attempt number.")
    if (not isinstance(pairs, list) or not pairs or any(not isinstance(pair, dict) for pair in pairs)
            or any(pair.get("case_id") != job.get("case_id") for pair in pairs)):
        raise ValueError("Local replay requires the matching non-empty frozen case-pair list.")
    if job.get("rule_ids") != [pair.get("base_rule_id") for pair in pairs]:
        raise ValueError("Frozen rule identifiers do not match the supplied case pairs.")
    if "pair_ids" in job and job["pair_ids"] != [pair.get("pair_id") for pair in pairs]:
        raise ValueError("Frozen pair identifiers do not match the supplied case pairs.")
    condition = job.get("condition")
    passages = pairs[0].get("passages") if condition != "A" else None
    system, user, refs = build_case_prompts(pairs, condition, passages,
                                           case_protocol=job.get("case_protocol", "requirement_declare_v1"))
    if "prompt_sha256" in job and job["prompt_sha256"] != p.digest([system, user]):
        raise ValueError("The case evidence differs from its frozen prompt hash.")
    _check_request_prompts(request, system, user)
    if job.get("provider") != request["provider"] or job.get("model") != request["payload"].get("model"):
        raise ValueError("Frozen provider/model identifiers disagree with the archived request.")
    _check_response_types(response)
    recovered_utc = p.utc_now()
    completed_utc, time_basis = _completion_time(response, started, recovered_utc)
    if response["status"] == "ok":
        result = parse_case_assessment(response["text"], job["rule_ids"], refs,
                                       case_protocol=job.get("case_protocol", "requirement_declare_v1"))
    else:
        result = {"status": response["status"], "assessments": [], "error": response.get("error"), "format_notes": []}
    result.update({
        "job_id": identifier, "request_sha256": job["request_sha256"],
        "completed_utc": completed_utc, "completion_time_basis": time_basis,
        "call_attempted": response.get("attempted"), "model_returned": response.get("model_returned"),
        "response_id": response.get("response_id"), "provider_request_id": response.get("request_id"),
        "usage": response.get("usage"), "elapsed_seconds": response.get("elapsed_seconds"),
        "http_status": response.get("http_status"), "finish_reason": response.get("finish_reason"),
        "raw_response_sha256": source_hashes[response_path],
        "recovered_from_response_archive": True, "recovered_utc": recovered_utc,
    })
    result.update(normalise_usage(job["provider"], response.get("usage")))
    if any(p.sha256_file(path) != digest for path, digest in source_hashes.items()):
        raise ValueError("Archived replay evidence changed during local recovery; no assessment was written.")
    if assessment_path.exists():
        return None
    p.write_json(assessment_path, result)
    return result
