"""Offline checks of synthetic DECLARE counterexamples.

Formula consistency is conditional on the supplied, unvalidated requirement and
world-fact interpretation. It does not establish legal truth, event mappings or
real-case outcomes. Failed witnesses and model labels are retained unchanged.
There are no provider calls, run scheduling or output-publication operations.

Event-list order is preserved through equal synthetic timestamps and source-row
positions. An empty witness uses one fresh unmatched neutral activity only for
the engine: all eight supported templates have the same truth on that encoding.
The model's original empty event list remains unchanged in the returned record.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
from pathlib import Path
import re
import tempfile
from typing import Any

import pandas as pd

from curia_conformance.engine import run_conformance
from curia_conformance.model import Rule, load_rules

TEMPLATES = frozenset({"AtLeastOne", "Init", "Precedence", "Response",
                       "RespondedExistence", "CoExistence", "NotCoExistence", "Succession"})
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
ALLOWED_REFS = frozenset({"REQUIREMENT", "DECLARE", "LOG_SCHEMA", "ACTIVITY_DICTIONARY", "SEMANTICS"})
WITNESS_FIELDS = frozenset({"target", "events", "world_facts", "obligation_engaged",
    "requirement_fulfilled", "formula_satisfied", "depends_on_unknown_mapping", "explanation", "evidence_refs"})


def parse_rule(rule_id: str, declaration: str) -> Rule:
    """Use the existing public model loader, preserving the literal formula."""
    if not isinstance(rule_id, str) or not _SAFE_ID.fullmatch(rule_id):
        raise ValueError("Unsafe synthetic rule identifier.")
    if not isinstance(declaration, str) or any(c in declaration for c in "\t\n\r"):
        raise ValueError("The DECLARE formula must be one literal line.")
    with tempfile.TemporaryDirectory(prefix="synthetic_witness_rule_") as temporary:
        path = Path(temporary) / "rule.tsv"
        path.write_text("ID\tDECLARE\n" + rule_id + "\t" + declaration + "\n", encoding="utf-8")
        rules = load_rules(path)
    if len(rules) != 1 or rules[0].template not in TEMPLATES:
        raise ValueError("Witness checking supports exactly the eight frozen template types.")
    return rules[0]


def evaluate_formula(rule: Rule, events: list[str]) -> bool:
    """Independent finite-position semantics for the eight audit templates."""
    if rule.template not in TEMPLATES or not isinstance(events, list) or any(not isinstance(x, str) for x in events):
        raise ValueError("Unsupported formula or event-list type.")
    first = [i for i, label in enumerate(events) if label == rule.activities[0]]
    if rule.template == "AtLeastOne":
        return bool(first)
    if rule.template == "Init":
        return bool(events) and events[0] == rule.activities[0]
    second = [i for i, label in enumerate(events) if label == rule.activities[1]]
    response = all(any(j > i for j in second) for i in first)
    precedence = all(any(i < j for i in first) for j in second)
    return {
        "Precedence": precedence,
        "Response": response,
        "RespondedExistence": not first or bool(second),
        "CoExistence": bool(first) == bool(second),
        "NotCoExistence": not (bool(first) and bool(second)),
        "Succession": response and precedence,
    }[rule.template]


def engine_formula(rule: Rule, events: list[str], synthetic_id: str) -> dict[str, Any]:
    """Evaluate one synthetic trace through the unchanged verified engine."""
    if not synthetic_id.startswith("SYNTHETIC_") or not _SAFE_ID.fullmatch(synthetic_id):
        raise ValueError("Engine traces require an unmistakably synthetic identifier.")
    encoded = list(events)
    neutral = None
    if not encoded:
        neutral = "SYNTHETIC_NEUTRAL"
        while neutral in rule.activities:
            neutral += "_X"
        encoded = [neutral]
    frame = pd.DataFrame([
        {"CaseID": synthetic_id, "Activity": activity,
         "Timestamp": pd.Timestamp("2000-01-01"), "_source_row": position}
        for position, activity in enumerate(encoded)
    ])
    # Declare4Py prints a progress line; keep CLI output limited to the report.
    from contextlib import redirect_stdout
    with redirect_stdout(io.StringIO()):
        pairs, _, metadata = run_conformance(frame, [rule], tie_break="source")
    if len(pairs) != 1 or pairs.iloc[0]["CaseID"] != synthetic_id or pairs.iloc[0]["constraint"] != rule.constraint:
        raise ValueError("The engine changed the synthetic trace or literal formula identity.")
    row = pairs.iloc[0]
    return {"formula_satisfied": bool(row["formal_satisfied"]),
            "formal_outcome": row["formal_outcome"],
            "activation_status": row["activation_status"],
            "vacuously_satisfied": bool(row["vacuously_satisfied"]),
            "empty_trace_neutral_encoding": not bool(events),
            "neutral_activity": neutral,
            "engine": metadata["engine"],
            "declare4py_version": metadata["declare4py_version"],
            "known_answer_contract_checks_passed": metadata["known_answer_contract_checks_passed"],
            "tie_break_policy": metadata["tie_break_policy"]}


def _direction(formula: bool, engaged: bool, fulfilled: bool | None) -> str | None:
    if engaged and isinstance(fulfilled, bool):
        if formula and not fulfilled:
            return "formula_weaker"
        if not formula and fulfilled:
            return "formula_stronger"
    elif not engaged and fulfilled is None and not formula:
        return "scope_overreach"
    return None


def check_counterexample(rule: Rule, witness: Any, allowed_labels: set[str], *,
                         index: int, job_id: str, engine_cache: dict | None = None) -> dict:
    """Preserve even a failed counterexample; return independent formula flags."""
    model = copy.deepcopy(witness)
    result: dict[str, Any] = {"witness_index": index, "model_counterexample": model,
        "events": copy.deepcopy(witness.get("events")) if isinstance(witness, dict) else None,
        "formula_satisfied_claim": witness.get("formula_satisfied") if isinstance(witness, dict) else None,
        "formula_satisfied_engine": None, "formula_satisfied_pure": None,
        "formula_claim_matches_engine": False, "engine_pure_agree": False,
        "formula_engine_status": "not_evaluable", "direction": None,
        "valid_in_scope_witness": False, "valid_ungated_witness": False,
        "requirement_truth_validated": False, "world_facts_validated": False,
        "flags": []}
    flags = result["flags"]
    if not isinstance(witness, dict) or set(witness) != WITNESS_FIELDS:
        flags.append("The counterexample is not an object with the exact required fields.")
        return result
    events = witness.get("events")
    if (not isinstance(events, list) or len(events) > 8
            or any(not isinstance(label, str) or label not in allowed_labels for label in events)):
        flags.append("Events are not an exact allowed-label list of length 0--8.")
        return result
    if not isinstance(witness.get("formula_satisfied"), bool):
        flags.append("The model formula claim is not Boolean.")
        return result
    target = witness.get("target")
    engaged, fulfilled = witness.get("obligation_engaged"), witness.get("requirement_fulfilled")
    unknown = witness.get("depends_on_unknown_mapping")
    if (not isinstance(target, str) or target not in {"in_scope", "ungated", "both"}
            or not isinstance(engaged, bool) or not isinstance(unknown, bool)
            or fulfilled is not None and not isinstance(fulfilled, bool)):
        flags.append("The counterexample has invalid categorical or Boolean field types.")
        return result
    facts, refs, explanation = witness.get("world_facts"), witness.get("evidence_refs"), witness.get("explanation")
    if (not isinstance(facts, list) or len(facts) > 10
            or any(not isinstance(x, str) or not x.strip() or len(x) > 500 for x in facts)
            or not isinstance(refs, list) or not 1 <= len(refs) <= 5
            or any(not isinstance(x, str) or x not in ALLOWED_REFS for x in refs)
            or len(refs) != len(set(refs)) or not isinstance(explanation, str)
            or not explanation.strip() or len(explanation) > 1500):
        flags.append("The counterexample has invalid fact, reference or explanation fields.")
        return result
    structurally_qualified = True
    if engaged and fulfilled is None:
        flags.append("An engaged requirement has no Boolean fulfilment claim."); structurally_qualified = False
    if not engaged and (fulfilled is not None or target != "ungated"):
        flags.append("A non-engaged obligation must target only ungated and have null fulfilment."); structurally_qualified = False
    token = hashlib.sha256(json.dumps([job_id, index, rule.rule_id, events], ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()[:20]
    synthetic_id = f"SYNTHETIC_{rule.rule_id}_{token}"
    result["synthetic_trace_id"] = synthetic_id
    try:
        pure = evaluate_formula(rule, events)
        result["formula_satisfied_pure"] = pure
        cache = engine_cache if engine_cache is not None else {}
        key = (rule.constraint, tuple(events))
        if key not in cache:
            cache[key] = engine_formula(rule, events, synthetic_id)
        engine = copy.deepcopy(cache[key])
        result["engine_check"] = engine
        result["formula_satisfied_engine"] = engine["formula_satisfied"]
        result["engine_pure_agree"] = engine["formula_satisfied"] == pure
        result["formula_claim_matches_engine"] = engine["formula_satisfied"] == witness["formula_satisfied"]
        result["formula_engine_status"] = "matched" if result["formula_claim_matches_engine"] else "mismatched"
        if not result["engine_pure_agree"]:
            flags.append("Independent pure semantics and Declare4Py disagree.")
        if not result["formula_claim_matches_engine"]:
            flags.append("The model's formula truth claim disagrees with the engine.")
        if structurally_qualified:
            direction = _direction(engine["formula_satisfied"], engaged, fulfilled)
            result["direction"] = direction
            if direction is None:
                flags.append("The stated facts do not yield a divergent direction.")
            if unknown:
                flags.append("The model marks dependence on an unknown event mapping.")
            base = result["engine_pure_agree"] and result["formula_claim_matches_engine"] and not unknown
            result["valid_in_scope_witness"] = bool(base and engaged and target in {"in_scope", "both"}
                                                     and direction in {"formula_weaker", "formula_stronger"})
            result["valid_ungated_witness"] = bool(base and target in {"ungated", "both"}
                                                    and direction in {"formula_weaker", "formula_stronger", "scope_overreach"})
    except Exception as exc:
        # A model's accepted response remains accepted even when its witness is
        # unsupported by this checker or the independent engine fails.
        result["formula_engine_status"] = "not_evaluable"
        result["engine_error"] = f"{type(exc).__name__}: {exc}"
        flags.append("Independent engine evaluation was not available; no formula-consistency claim is made.")
    return result


def _head_consistency(head: Any, witnesses: list[dict], primary: bool) -> tuple[bool, list[int]]:
    if not isinstance(head, dict) or head.get("outcome") != "Disagreement":
        return False, []
    relation = head.get("relation")
    if not isinstance(relation, str):
        return False, []
    wanted = {"formula_weaker", "formula_stronger"} if relation == "incomparable" else {relation}
    if not wanted.issubset({"formula_weaker", "formula_stronger"} if primary else
                          {"formula_weaker", "formula_stronger", "scope_overreach"}):
        return False, []
    eligible = [w for w in witnesses if w["valid_in_scope_witness" if primary else "valid_ungated_witness"]
                and w["direction"] in wanted]
    return wanted.issubset({w["direction"] for w in eligible}), [w["witness_index"] for w in eligible]


def check_job(job: dict, rule_row: dict, assessment: dict, allowed_labels: set[str], *,
              engine_cache: dict | None = None) -> dict:
    rule_id = rule_row.get("base_rule_id", rule_row.get("rule_id"))
    if job["rule_id"] != rule_id:
        raise ValueError("Witness rule identity differs from the frozen job.")
    rule = parse_rule(rule_id, rule_row["declare"])
    counterexamples = assessment.get("counterexamples")
    witnesses = [check_counterexample(rule, value, allowed_labels, index=index,
                    job_id=job["job_id"], engine_cache=engine_cache)
                 for index, value in enumerate(counterexamples, 1)] if isinstance(counterexamples, list) else []
    heads = {name: assessment.get(name) for name in ("in_scope_assessment", "ungated_assessment")}
    primary, primary_indices = _head_consistency(heads["in_scope_assessment"], witnesses, True)
    secondary, secondary_indices = _head_consistency(heads["ungated_assessment"], witnesses, False)
    logical_flags = []
    if not isinstance(counterexamples, list) or len(counterexamples) > 3:
        logical_flags.append("The counterexample collection is not a list of at most three witnesses.")
    for name, head, consistent in (("in_scope_assessment", heads["in_scope_assessment"], primary),
                                  ("ungated_assessment", heads["ungated_assessment"], secondary)):
        if not isinstance(head, dict):
            logical_flags.append(f"{name} is unavailable or is not an object.")
            continue
        outcome, relation = head.get("outcome"), head.get("relation")
        permitted = {"Agreement": {"equivalent"}, "Undetermined": {"unresolved"},
                     "Disagreement": {"formula_weaker", "formula_stronger", "incomparable"}}
        if name == "ungated_assessment":
            permitted["Disagreement"].add("scope_overreach")
        if (not isinstance(outcome, str) or outcome not in permitted or not isinstance(relation, str)
                or relation not in permitted[outcome]):
            logical_flags.append(f"{name} has an inconsistent or unavailable outcome/relation combination.")
        if outcome == "Disagreement" and not consistent:
            logical_flags.append(f"{name} Disagreement lacks formula-consistent, scope/mapping-qualified witnesses in its stated direction.")
    return {"rule_id": rule_id, "provider_id": job["provider_id"], "model": job["model"],
            "request_sha256": job["request_sha256"],
            "raw_response_sha256": assessment["raw_response_sha256"],
            "attempt": assessment["attempt"],
            "declare": rule_row["declare"], "requirement": rule_row["requirement"],
            "model_in_scope_assessment": copy.deepcopy(heads["in_scope_assessment"]),
            "model_ungated_assessment": copy.deepcopy(heads["ungated_assessment"]),
            "model_counterexamples": copy.deepcopy(counterexamples),
            "logical_flags": logical_flags,
            "witnesses": witnesses,
            "in_scope_formula_consistent_D": primary,
            "ungated_formula_consistent_D": secondary,
            "in_scope_supporting_witness_indices": primary_indices,
            "ungated_supporting_witness_indices": secondary_indices,
            "requirement_truth_validated": False, "world_facts_validated": False}

