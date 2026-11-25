"""Batch a case's requirements without exposing checker or model metadata.

Each rule retains its own requirement, DECLARE formula and independent judgement.
Shared case evidence is projected through the existing single-rule whitelist, and individual answers
use the existing strict assessment validator.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .prompts import (
    SYSTEM_PROMPT,
    _append_document_data,
    _frozen_document_data,
    _InvalidJSON,
    _reject_constant,
    _unique_keys,
    build_prompts,
    parse_assessment,
)


CASE_SYSTEM_PROMPT = (
    SYSTEM_PROMPT.split("Return only one JSON object", 1)[0]
    .replace(
        "Assess one supplied procedural requirement for one judicial case",
        "Assess each supplied procedural requirement separately for one judicial case",
    )
    .rstrip()
    + """

Assess every listed rule independently using its own supplied requirement and DECLARE formula. Do not transfer a conclusion or a legal premise from one rule to another. The evidence identifier REQUIREMENT is scoped to the rule being assessed: citing REQUIREMENT in a rule's answer refers only to that rule's requirement text, never another rule's requirement. DECLARE is also scoped to the rule being assessed and refers only to its supplied formula, never another rule's formula or a checker result. CASE, E-prefixed event identifiers and document-passage identifiers refer to the shared case evidence.

Return only one JSON object with exactly the field "assessments". Its value must be a list containing exactly one object for each requested rule_id, with no missing, repeated or additional rule identifiers. Each object must contain exactly "rule_id", "outcome", "evidence_refs" and "justification". Copy the requested rule_id exactly. Use the three outcome labels exactly as defined above. "evidence_refs" must be a list of distinct identifiers from the supplied reference list. Do not invent quotations, facts or references. Use an empty reference list only when no supplied item can be cited. Each "justification" must be a concise, non-empty explanation in British English, no longer than 2,000 characters, referring to that rule's decisive evidence or evidential gap. Do not give step-by-step reasoning, confidence scores, Markdown fences or information identifying the model or provider."""
)


def _rule_identifier(value: Any) -> str:
    if (not isinstance(value, str) or not value or value != value.strip()
            or any(character.isspace() for character in value)):
        raise ValueError("rule_id must be a non-empty identifier without whitespace.")
    return value


def _evidence_body(user: str) -> dict[str, Any]:
    return json.loads(user.split("EVIDENCE DATA BEGIN\n", 1)[1].rsplit("\nEVIDENCE DATA END", 1)[0])


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)


def build_case_prompts(
    pairs: list[dict[str, Any]], condition: str = "A",
    passages: list[dict[str, Any]] | None = None,
    *, case_protocol: str = "requirement_declare_v1",
) -> tuple[str, str, set[str]]:
    """Present one case once, with separately identified requirements and formulae.

    All pairs must describe the same projected case. There must be exactly one
    formulation per base rule. Extra pair fields remain outside the evidence.
    Phase B preserves the entire phase-A user prompt as an exact prefix and
    appends only a document-data block with its expanded evidence references.
    """
    if case_protocol == "critical_v1":
        from .critical_prompts import build_critical_case_prompts
        return build_critical_case_prompts(pairs, condition, passages)
    if case_protocol != "requirement_declare_v1":
        raise ValueError("Unknown case_protocol.")
    if condition == "B":
        system, baseline, baseline_refs = build_case_prompts(pairs, "A", None)
        data, allowed_refs = _frozen_document_data(passages, baseline_refs)
        return system, _append_document_data(baseline, data), allowed_refs
    if not isinstance(pairs, list) or not pairs:
        raise ValueError("Case assessment requires a non-empty list of case--rule pairs.")
    rules, identifiers = [], set()
    common, allowed_refs, instruction = None, None, None
    for pair in pairs:
        if not isinstance(pair, dict):
            raise ValueError("Each case--rule pair must be an object.")
        rule_id = _rule_identifier(pair.get("base_rule_id"))
        if rule_id in identifiers:
            raise ValueError("Case assessment requires one formulation per distinct rule_id.")
        identifiers.add(rule_id)
        _, single_user, refs = build_prompts(pair, condition, passages)
        projected = _evidence_body(single_user)
        evidence = {key: projected[key] for key in ("case", "additional_passages") if key in projected}
        if common is None:
            common, allowed_refs = evidence, refs
            instruction = single_user.split("\n\nAllowed evidence identifiers:", 1)[0]
        elif _canonical(common) != _canonical(evidence) or allowed_refs != refs:
            raise ValueError("Every rule in a case assessment must use the same case evidence.")
        rules.append({
            "rule_id": rule_id,
            "requirement": projected["requirement"],
            "declare": projected["declare"],
        })
    dossier = {**common, "rules": rules}
    user = (
        instruction.replace("Assess the requirement", "Assess every listed requirement independently")
        + "\n\nAllowed evidence identifiers: "
        + json.dumps(sorted(allowed_refs), ensure_ascii=False)
        + "\n\nEVIDENCE DATA BEGIN\n"
        + json.dumps(dossier, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2)
        + "\nEVIDENCE DATA END"
    )
    return CASE_SYSTEM_PROMPT, user, allowed_refs


def parse_case_assessment(
    text: str, rule_ids: list[str], allowed_refs: set[str],
    *, case_protocol: str = "requirement_declare_v1",
) -> dict[str, Any]:
    """Validate a batch and retain independently valid rule answers.

    An ambiguous or incomplete rule allocation invalidates the whole batch.
    With a complete unique allocation, each rule is validated independently;
    one invalid substantive answer cannot turn another into a missing result.
    Raw answers are never repaired or inferred.
    """
    if case_protocol == "critical_v1":
        from .critical_prompts import parse_critical_case_assessment
        return parse_critical_case_assessment(text, rule_ids, allowed_refs)
    if case_protocol != "requirement_declare_v1":
        raise ValueError("Unknown case_protocol.")
    result: dict[str, Any] = {
        "status": "invalid_output", "assessments": [], "error": None, "format_notes": [],
    }
    try:
        if not isinstance(rule_ids, list) or not rule_ids:
            raise ValueError("rule_ids must be a non-empty list of unique identifiers.")
        expected = [_rule_identifier(identifier) for identifier in rule_ids]
        if len(expected) != len(set(expected)):
            raise ValueError("rule_ids must be a non-empty list of unique identifiers.")
    except ValueError as exc:
        result["error"] = str(exc)
        return result

    def invalid(status: str, detail: str) -> dict[str, Any]:
        result.update(status=status, error=detail)
        result["assessments"] = [{
            "rule_id": identifier, "status": status, "outcome": None,
            "evidence_refs": [], "justification": None, "error": detail, "format_notes": [],
        } for identifier in expected]
        return result

    if not isinstance(text, str):
        return invalid("invalid_output", "The response text must be a string.")
    if not isinstance(allowed_refs, (set, frozenset)) or any(not isinstance(ref, str) for ref in allowed_refs):
        return invalid("invalid_output", "Allowed evidence references must be a set of strings.")
    content = text.strip()
    fence = re.fullmatch(r"```(?:json)?[ \t]*\r?\n(.*?)\r?\n```", content, flags=re.DOTALL | re.IGNORECASE)
    if fence:
        content = fence.group(1)
        result["format_notes"].append("Removed one enclosing Markdown code fence.")
    try:
        parsed = json.loads(content, object_pairs_hook=_unique_keys, parse_constant=_reject_constant)
        # An overflowing numeric literal such as 1e999 also becomes non-finite.
        _canonical(parsed)
    except (ValueError, RecursionError) as exc:
        if isinstance(exc, json.JSONDecodeError):
            detail = f"Invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}."
        elif isinstance(exc, RecursionError):
            detail = "The JSON nesting depth is excessive."
        elif isinstance(exc, _InvalidJSON):
            detail = str(exc)
        else:
            detail = "The JSON contains an unsupported value."
        return invalid("invalid_json", detail)
    if not isinstance(parsed, dict) or set(parsed) != {"assessments"} or not isinstance(parsed["assessments"], list):
        return invalid("invalid_output", "Expected exactly assessments containing a list in one JSON object.")
    records = parsed["assessments"]
    if any(not isinstance(record, dict) or not isinstance(record.get("rule_id"), str) for record in records):
        return invalid("invalid_output", "Every assessment must be an object with a rule_id string.")
    actual = [record["rule_id"] for record in records]
    if len(actual) != len(set(actual)) or set(actual) != set(expected):
        return invalid("invalid_output", "Assessments must name exactly every requested rule_id once, without extra identifiers.")
    by_identifier = {record["rule_id"]: record for record in records}
    for identifier in expected:
        record = by_identifier[identifier]
        payload = {key: value for key, value in record.items() if key != "rule_id"}
        validation = parse_assessment(json.dumps(payload, ensure_ascii=False, allow_nan=False), allowed_refs)
        result["assessments"].append({"rule_id": identifier, **validation})
    if all(record["status"] == "ok" for record in result["assessments"]):
        result.update(status="ok", error=None)
    else:
        result["error"] = "One or more rule assessments failed validation."
    return result
